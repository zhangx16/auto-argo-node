"""Bounded panel-side VLESS-over-WebSocket + HTTPS probe; no node-side client core."""
import socket
import ssl
import struct
import time
from urllib.parse import urlparse, parse_qs, urlsplit, urlunsplit
from uuid import UUID
from websockets.sync.client import connect


def probe_link(link, timeout=15):
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
