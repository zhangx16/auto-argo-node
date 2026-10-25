"""Bounded panel-side VLESS-over-WebSocket + HTTPS probe; no node-side client core."""
import socket
import ssl
import struct
import time
from urllib.parse import urlparse, parse_qs, urlsplit, urlunsplit
from uuid import UUID
from websockets.sync.client import connect


def probe_link(link, timeout=15):
    u=urlparse(link);q=parse_qs(u.query)
    if u.scheme in ('hysteria2','hy2') or q.get('security')==['reality']:
        return native_probe(link,timeout)
    return websocket_probe(link,timeout)


def native_probe(link,timeout=15):
    from . import runtime_budget
    with runtime_budget.slot():return _native_probe(link,timeout)


def _native_probe(link,timeout=15):
    import json,os,subprocess,tempfile
    from pathlib import Path
    from .node_formats import parse_uri,singbox_outbound
    start=time.monotonic()
    outbound=singbox_outbound(parse_uri(link));outbound['tag']='node'
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    config={'log':{'level':'error'},'dns':{'servers':[{'type':'local','tag':'dns-direct'}]},
            'inbounds':[{'type':'mixed','listen':'127.0.0.1','listen_port':port}],
            'outbounds':[outbound],'route':{'final':'node','default_domain_resolver':{'server':'dns-direct','strategy':'prefer_ipv4'}}}
    with tempfile.TemporaryDirectory(prefix='node-probe-') as temp:
        file=Path(temp)/'config.json';file.write_text(json.dumps(config));file.chmod(0o600)
        with open(os.devnull,'wb') as quiet:
            process=subprocess.Popen(['/usr/local/bin/sing-box-probe','run','-c',str(file)],stdout=quiet,stderr=quiet,env=os.environ|{'GOMAXPROCS':'1'})
            try:
                for _ in range(40):
                    if process.poll() is not None:raise ValueError('探测客户端启动失败')
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                    except OSError:time.sleep(.05)
                result=subprocess.run(['curl','--noproxy','','--proxy',f'http://127.0.0.1:{port}','--connect-timeout','5','--max-time',str(timeout),'-fsS','-o',os.devnull,'-w','%{http_code}','https://www.gstatic.com/generate_204'],stdout=subprocess.PIPE,stderr=quiet,timeout=timeout+2)
                status=204
                if result.returncode or result.stdout!=b'204':
                    # A blocked/slow Google endpoint alone must not restart a healthy node.
                    alternate=subprocess.run(['curl','--noproxy','','--proxy',f'http://127.0.0.1:{port}','--connect-timeout','5','--max-time',str(timeout),'--max-filesize','4096','-fsS','-w','\n%{http_code}','https://www.cloudflare.com/cdn-cgi/trace'],stdout=subprocess.PIPE,stderr=quiet,timeout=timeout+2)
                    body,_,code=alternate.stdout.rpartition(b'\n')
                    if alternate.returncode or code!=b'200' or not any(line.startswith(b'ip=') for line in body.splitlines()):raise ValueError('协议握手或两处 HTTPS 实际转发均未通过（检查公网地址、映射端口和 TCP/UDP 放行）')
                    status=200
                return {'ok':True,'latency_ms':round((time.monotonic()-start)*1000),'http_status':status}
            finally:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait()


def websocket_probe(link, timeout=15):
    start=time.monotonic();deadline=start+timeout
    u=urlparse(link);q=parse_qs(u.query)
    if u.scheme!='vless' or q.get('type')!=['ws'] or q.get('security')!=['tls']:
        raise ValueError('仅支持 VLESS-WS-TLS 探测')
    domain=q['sni'][0];ws_path=q['path'][0]
    path_parts=urlsplit(ws_path)
    # Early-data URL options are client hints, not part of the origin path.
    path_query='&'.join(p for p in path_parts.query.split('&') if p and not p.startswith('ed='))
    ws_path=urlunsplit(('', '',path_parts.path,path_query,''))
    sock=socket.create_connection((u.hostname,u.port or 443),timeout=min(5,timeout))
    try:
        ws=connect('wss://'+domain+ws_path,sock=sock,ssl=ssl.create_default_context(),server_hostname=domain,
                   proxy=None,compression=None,open_timeout=max(.1,deadline-time.monotonic()),
                   ping_interval=None,close_timeout=.5,max_size=131072,max_queue=4,legacy=True)
    except Exception:
        sock.close();raise
    try:
        target='www.gstatic.com';target_bytes=target.encode()
        # VLESS v0, UUID, no addons, TCP, port 443, domain address.
        ws.send(b'\x00'+UUID(u.username).bytes+b'\x00\x01'+struct.pack('!H',443)+b'\x02'+bytes([len(target_bytes)])+target_bytes)
        incoming=ssl.MemoryBIO();outgoing=ssl.MemoryBIO()
        tls=ssl.create_default_context().wrap_bio(incoming,outgoing,server_hostname=target)
        prefix=bytearray();header_done=False
        def flush():
            if outgoing.pending:ws.send(outgoing.read())
        def receive():
            nonlocal header_done
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('节点转发探测超时')
                packet=ws.recv(timeout=remaining)
                if not isinstance(packet,bytes):raise ValueError('VLESS 返回了非二进制数据')
                if not header_done:
                    prefix.extend(packet)
                    if len(prefix)<2:continue
                    if prefix[0]!=0:raise ValueError('VLESS 响应版本错误')
                    length=2+prefix[1]
                    if len(prefix)<length:continue
                    packet=bytes(prefix[length:]);prefix.clear();header_done=True
                if packet:incoming.write(packet);return
        while True:
            try:tls.do_handshake();flush();break
            except ssl.SSLWantReadError:flush();receive()
        request=(f'GET /generate_204 HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n').encode()
        tls.write(request);flush();response=b''
        while b'\r\n\r\n' not in response and len(response)<8192:
            try:
                chunk=tls.read(4096)
                if not chunk:break
                response+=chunk
            except ssl.SSLWantReadError:flush();receive()
        line=response.split(b'\r\n',1)[0]
        if not line.startswith(b'HTTP/1.') or len(line.split())<2:raise ValueError('节点未返回有效 HTTP 响应')
        status=int(line.split()[1])
        if not 200<=status<400:raise ValueError('转发测试返回 HTTP '+str(status))
        return {'ok':True,'latency_ms':round((time.monotonic()-start)*1000),'http_status':status}
    finally:ws.close()
