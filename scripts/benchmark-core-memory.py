#!/usr/bin/env python3
"""Loopback-only core benchmark. Temporary credentials; never touches deployed nodes.

Supply two server binaries and a separate sing-box client binary. Each run verifies
HTTPS downloads/uploads and SOCKS UDP, samples /proc RSS/PSS, then checks that
stopping the tested server makes requests fail (no direct-route bypass).
"""
import argparse
import concurrent.futures
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import shutil
import socketserver
import ssl
import statistics
import struct
import subprocess
import tempfile
import threading
import time
import uuid

BLOCK = bytes(range(256)) * 128
PAYLOAD = 8 * 1024 * 1024
DIGEST = hashlib.sha256(BLOCK * (PAYLOAD // len(BLOCK))).hexdigest()

class Origin(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def do_GET(self):
        count = PAYLOAD if self.path == '/download' else 1024
        self.send_response(200); self.send_header('Content-Length', str(count)); self.end_headers()
        try:
            for offset in range(0, count, len(BLOCK)):
                data = BLOCK[:min(len(BLOCK), count-offset)]
                self.wfile.write(data); self.wfile.flush()
                if count == PAYLOAD: time.sleep(len(data)/(2*1024*1024))
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError): pass
    def do_POST(self):
        left = int(self.headers['Content-Length']); digest = hashlib.sha256()
        while left:
            data = self.rfile.read(min(left, len(BLOCK)))
            if not data: return
            digest.update(data); left -= len(data)
        body = digest.hexdigest().encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

class LocalHTTPServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Reality intentionally replaces the decoy TLS handshake; its discarded
        # connection can end with a TLS record error on this local decoy server.
        pass


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        sock.sendto(data, self.client_address)


def port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


def connect(proxy_port, origin_port, timeout=10):
    c = http.client.HTTPSConnection('127.0.0.1', proxy_port, timeout=timeout, context=ssl._create_unverified_context())
    c.set_tunnel('127.0.0.1', origin_port)
    return c


def download(proxy_port, origin_port, small=False):
    c = connect(proxy_port, origin_port, 3 if small else 15)
    try:
        c.request('GET', '/small' if small else '/download')
        r = c.getresponse(); assert r.status == 200, r.status
        h = hashlib.sha256(); count = 0
        while data := r.read(65536): h.update(data); count += len(data)
        assert count == (1024 if small else PAYLOAD), count
        if not small: assert h.hexdigest() == DIGEST
        return count
    finally: c.close()


def upload(proxy_port, origin_port):
    c = connect(proxy_port, origin_port)
    try:
        c.putrequest('POST', '/upload'); c.putheader('Content-Length', str(PAYLOAD)); c.endheaders()
        for _ in range(PAYLOAD // len(BLOCK)):
            c.send(BLOCK); time.sleep(len(BLOCK)/(2*1024*1024))
        r = c.getresponse(); assert r.status == 200 and r.read().decode() == DIGEST
        return PAYLOAD
    finally: c.close()


def receive(sock, n):
    out = b''
    while len(out) < n:
        part = sock.recv(n-len(out))
        if not part: raise RuntimeError('SOCKS closed early')
        out += part
    return out


def udp_check(proxy_port, echo_port):
    with socket.create_connection(('127.0.0.1', proxy_port), timeout=5) as control:
        control.sendall(b'\x05\x01\x00'); assert receive(control, 2) == b'\x05\x00'
        control.sendall(b'\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00')
        header = receive(control, 4); assert header[:2] == b'\x05\x00', header
        if header[3] == 1: host = socket.inet_ntoa(receive(control, 4))
        elif header[3] == 4: host = socket.inet_ntop(socket.AF_INET6, receive(control, 16))
        else: host = receive(control, receive(control, 1)[0]).decode()
        relay_port = struct.unpack('!H', receive(control, 2))[0]
        if host == '0.0.0.0': host = '127.0.0.1'
        payload = b'argo-core-benchmark-udp-' + os.urandom(32)
        packet = b'\x00\x00\x00\x01' + socket.inet_aton('127.0.0.1') + struct.pack('!H', echo_port) + payload
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(5); udp.sendto(packet, (host, relay_port)); result, _ = udp.recvfrom(4096)
            assert result.endswith(payload), 'UDP payload mismatch'


def sample(pid):
    result = {}
    for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines():
        key, _, val = line.partition(':')
        if key in ('Rss', 'Pss', 'Private_Clean', 'Private_Dirty', 'Swap'):
            result[key.lower()+'_kib'] = int(val.split()[0])
    fields = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()
    result['cpu_seconds'] = (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
    return result


def stop(process):
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


def config(kind, binary, work, certificate, key, handshake_port, keel=False):
    server_port = port(socket.SOCK_DGRAM if kind == 'hysteria2' else socket.SOCK_STREAM)
    proxy_port = port(); credential = str(uuid.uuid4())
    inbound = dict(type='hysteria2' if kind=='hysteria2' else 'vless', tag='benchmark', listen='127.0.0.1', listen_port=server_port)
    outbound = dict(type=inbound['type'], tag='proxy', server='127.0.0.1', server_port=server_port)
    if kind == 'hysteria2':
        inbound.update(users=[dict(password=credential)],tls=dict(enabled=True,certificate_path=certificate,key_path=key))
        outbound.update(password=credential,tls=dict(enabled=True,server_name='bench.test',insecure=True))
    else:
        inbound['users'] = [dict(uuid=credential)]; outbound.update(uuid=credential,packet_encoding='xudp')
        if kind == 'vless-ws':
            inbound['transport'] = outbound['transport'] = dict(type='ws',path='/benchmark')
        else:
            keys = subprocess.check_output([binary,'generate','reality-keypair'],text=True)
            keys = dict(line.split(': ',1) for line in keys.strip().splitlines())
            short = '0011223344556677'
            inbound['users'][0]['flow'] = outbound['flow'] = 'xtls-rprx-vision'
            inbound['tls'] = dict(enabled=True,server_name='bench.test',reality=dict(enabled=True,handshake=dict(server='127.0.0.1',server_port=handshake_port),private_key=keys['PrivateKey'],short_id=[short]))
            outbound['tls'] = dict(enabled=True,server_name='bench.test',utls=dict(enabled=True,fingerprint='chrome'),reality=dict(enabled=True,public_key=keys['PublicKey'],short_id=short))
    server = dict(log=dict(level='error'),inbounds=[inbound],outbounds=[dict(type='direct',tag='direct')],route=dict(final='direct'))
    client = dict(log=dict(level='error'),inbounds=[dict(type='mixed',tag='test-client',listen='127.0.0.1',listen_port=proxy_port)],outbounds=[outbound],route=dict(final='proxy'))
    if keel:
        assert kind == 'reality', 'Keel reference covers Reality only'
        reality=inbound['tls']['reality']
        server=dict(route=dict(allow_private=True),inbounds=[dict(type='vless',listen=f'127.0.0.1:{server_port}',reality=dict(dest=f'127.0.0.1:{handshake_port}',server_names=['bench.test'],private_key=reality['private_key'],short_ids=reality['short_id']))],users=[dict(name='benchmark',uuid=credential,flow='xtls-rprx-vision')])
    for name, value in [('server',server),('client',client)]:
        p=work/(name+'.json');p.write_text(json.dumps(value));p.chmod(0o600)
    return proxy_port


def run_case(kind, label, binary, client_binary, work, cert, key, origin_port, echo_port, repeat, quick=False):
    proxy_port = config(kind, client_binary, work, cert, key, origin_port, keel=label=='keel')
    env = {k:v for k,v in os.environ.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy') and k not in ('GOGC','GOMEMLIMIT','GODEBUG')}
    env['GOMAXPROCS'] = '1'
    logs = [(work/'server.log').open('w'),(work/'client.log').open('w')]
    server = client = None
    try:
        server_args=[binary,'-c',str(work/'server.json'),'-state',str(work/'keel-usage.json')] if label=='keel' else [binary,'run','-c',str(work/'server.json')]
        server=subprocess.Popen(server_args,env=env,stdout=logs[0],stderr=logs[0])
        client=subprocess.Popen([client_binary,'run','-c',str(work/'client.json')],env=env,stdout=logs[1],stderr=logs[1])
        time.sleep(1)
        cold_idle=sample(server.pid)
        for _ in range(40):
            if server.poll() is not None or client.poll() is not None: raise RuntimeError('Core exited at startup')
            try:
                download(proxy_port,origin_port,True);break
            except (OSError,http.client.HTTPException):time.sleep(.1)
        else: raise RuntimeError('Protocol readiness timed out')
        udp_check(proxy_port,echo_port)
        time.sleep(1 if quick else 5)
        idle=sample(server.pid); samples=[]
        start=time.monotonic(); before=sample(server.pid)['cpu_seconds']
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            def transfer(): return download(proxy_port,origin_port)+upload(proxy_port,origin_port)
            futures=[pool.submit(transfer) for _ in range(2 if quick else 8)]
            while not all(f.done() for f in futures):
                samples.append(sample(server.pid));time.sleep(.1)
            total=sum(f.result() for f in futures)
        duration=time.monotonic()-start; cpu=sample(server.pid)['cpu_seconds']-before
        time.sleep(1 if quick else 15)
        released=sample(server.pid)
        stop(client)  # Release the persistent Hysteria2 QUIC session, too.
        time.sleep(1 if quick else 5)
        disconnected=sample(server.pid)
        # Restart the client with its existing configuration, then stop the server.
        client=subprocess.Popen([client_binary,'run','-c',str(work/'client.json')],env=env,stdout=logs[1],stderr=logs[1]);time.sleep(.3)
        download(proxy_port,origin_port,True)  # Confirm the restarted client is ready before the negative check.
        stop(server)
        try:download(proxy_port,origin_port,True)
        except (OSError,http.client.HTTPException):bypass_check=True
        else:raise RuntimeError('Request bypassed the stopped server')
        return dict(protocol=kind,variant=label,repeat=repeat,cold_idle=cold_idle,idle=idle,active_median={k:statistics.median(s[k] for s in samples) for k in ('rss_kib','pss_kib')},active_peak={k:max(s[k] for s in samples) for k in ('rss_kib','pss_kib')},released_15s=released,client_closed_5s=disconnected,cpu_seconds=round(cpu,3),transfer_seconds=round(duration,3),verified_bytes=total,udp_ok=True,bypass_check=bypass_check)
    except Exception as exc:
        for f in logs:f.flush()
        details='\n'.join((work/(name+'.log')).read_text()[-2000:] for name in ['server','client'])
        raise RuntimeError(f'{label}/{kind}: {exc}\n{details}') from exc
    finally:
        for p in [client,server]:
            if p is not None:stop(p)
        for f in logs:f.close()



def idle_cases(binaries, client_binary, work, cert, key, origin_port, echo_port, seconds, protocols, out, results):
    """Keep six independent cores idle, with separate executable inodes for PSS."""
    cases=[];env={k:v for k,v in os.environ.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy') and k not in ('GOGC','GOMEMLIMIT','GODEBUG')};env['GOMAXPROCS']='1'
    try:
        for kind in protocols:
            for label,binary in binaries:
                d=work/(kind+'-'+label);d.mkdir();own=d/'core';shutil.copy2(binary,own)
                proxy=config(kind,client_binary,d,cert,key,origin_port)
                log=(d/'core.log').open('w')
                server=subprocess.Popen([str(own),'run','-c',str(d/'server.json')],env=env,stdout=log,stderr=log)
                row=dict(protocol=kind,variant=label,samples=[]);case=dict(server=server,client=None,log=log,row=row);cases.append(case)
                client=subprocess.Popen([client_binary,'run','-c',str(d/'client.json')],env=env,stdout=log,stderr=log);case['client']=client
                for _ in range(40):
                    if server.poll() is not None or client.poll() is not None:raise RuntimeError('Idle core failed to start')
                    try:download(proxy,origin_port,True);break
                    except (OSError,http.client.HTTPException):time.sleep(.1)
                else:raise RuntimeError('Idle readiness failed')
                udp_check(proxy,echo_port);stop(client)
                case['since']=time.monotonic();row['samples'].append(dict(age_seconds=0,**sample(server.pid)))
        for age in (30,seconds):
            for case in cases:
                while time.monotonic()-case['since']<age:time.sleep(min(1,age-(time.monotonic()-case['since'])))
                row=case['row'];row['samples'].append(dict(age_seconds=round(time.monotonic()-case['since'],1),**sample(case['server'].pid)))
                print(json.dumps(dict(idle_protocol=row['protocol'],variant=row['variant'],age=age,rss_mib=round(row['samples'][-1]['rss_kib']/1024,2))),flush=True)
            results['idle_runs']=[c['row'] for c in cases];out.write_text(json.dumps(results,indent=2))
    finally:
        for case in cases:
            for proc in [case['client'],case['server']]:
                if proc is not None:stop(proc)
            case['log'].close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--baseline',required=True);parser.add_argument('--candidate',required=True);parser.add_argument('--client',required=True);parser.add_argument('--output',required=True);parser.add_argument('--repeats',type=int,default=3);parser.add_argument('--quick',action='store_true');parser.add_argument('--idle-seconds',type=int,default=0);parser.add_argument('--keel');parser.add_argument('--variants',nargs='+',choices=['official','minimal','keel']);parser.add_argument('--protocols',nargs='+',default=['vless-ws','reality','hysteria2'],choices=['vless-ws','reality','hysteria2']);args=parser.parse_args()
    binaries=[('official',str(Path(args.baseline).resolve())),('minimal',str(Path(args.candidate).resolve()))]
    if args.keel:binaries.append(('keel',str(Path(args.keel).resolve())))
    if args.variants:binaries=[(label,path) for label,path in binaries if label in args.variants]
    if not binaries:parser.error('No binaries selected')
    if args.idle_seconds and args.keel:parser.error('Long-idle mode currently covers sing-box only')
    if args.idle_seconds and args.idle_seconds<30:parser.error('Idle duration must be at least 30 seconds')
    if args.repeats<1:parser.error('At least one repeat is required')
    results=dict(environment=dict(mode='idle' if args.idle_seconds else 'transfer',idle_seconds=args.idle_seconds,architecture=os.uname().machine,gomaxprocs=1,gogc='default',gomemlimit='unset',connections=0 if args.idle_seconds else (2 if args.quick else 8),scope='loopback HTTPS and UDP; no cloudflared'),binaries={label:dict(bytes=Path(path).stat().st_size,sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),version=subprocess.check_output([path,'version'],text=True) if label!='keel' else 'Keel Reality-only reference; binary identified by SHA256') for label,path in binaries},runs=[])
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='argo-core-bench.') as tmp:
        work=Path(tmp);cert=str(work/'cert.pem');key=str(work/'key.pem')
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',key,'-out',cert,'-days','1','-subj','/CN=bench.test','-addext','subjectAltName=DNS:bench.test'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_3;context.load_cert_chain(cert,key);context.set_alpn_protocols(['http/1.1'])
        origin=LocalHTTPServer(('127.0.0.1',0),Origin);origin.socket=context.wrap_socket(origin.socket,server_side=True)
        echo=socketserver.ThreadingUDPServer(('127.0.0.1',0),Echo)
        for service in [origin,echo]:threading.Thread(target=service.serve_forever,daemon=True).start()
        try:
            if args.idle_seconds:
                idle_cases(binaries,args.client,work,cert,key,origin.server_port,echo.server_address[1],args.idle_seconds,args.protocols,out,results)
                return
            for repeat in range(1,args.repeats+1):
                for kind in args.protocols:
                    for label,binary in (binaries if repeat%2 else list(reversed(binaries))):
                        if label=='keel' and kind!='reality':continue
                        result=run_case(kind,label,binary,args.client,work,cert,key,origin.server_port,echo.server_address[1],repeat,args.quick)
                        results['runs'].append(result);out.write_text(json.dumps(results,indent=2))
                        print(json.dumps(dict(protocol=kind,variant=label,repeat=repeat,idle_rss_mib=round(result['idle']['rss_kib']/1024,2),active_rss_mib=round(result['active_median']['rss_kib']/1024,2),udp_ok=True,verified_mib=result['verified_bytes']//1048576)),flush=True)
        finally:
            for service in [origin,echo]:service.shutdown();service.server_close()
    print('Benchmark complete:',out,flush=True)

if __name__=='__main__':main()
