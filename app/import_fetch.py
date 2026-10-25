"""Bounded public HTTP import with per-hop DNS pinning; no ambient proxy use."""
import gzip
import http.client
import io
import ipaddress
import socket
import ssl
import time
import zlib
from urllib.parse import quote, urljoin, urlsplit

MAX_BYTES = 1024 * 1024


def public_target(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError('订阅 URL 格式无效')
    try:
        u = urlsplit(url)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username is not None or u.password is not None:
            raise ValueError()
        port = u.port or (443 if u.scheme == 'https' else 80)
        host = u.hostname.encode('idna').decode('ascii')
        if '%' in host or not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ValueError('仅支持有效的 HTTP/HTTPS 订阅 URL')
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('订阅服务器 DNS 解析失败')
    ips = list(dict.fromkeys(item[4][0] for item in addresses))
    def allowed(ip):
        addr = ipaddress.ip_address(ip)
        return addr.is_global and not addr.is_multicast and not (addr.version == 6 and (addr.ipv4_mapped or addr.sixtofour or addr.teredo))
    if not ips or not all(allowed(ip) for ip in ips):
        raise ValueError('订阅 URL 只能指向公网地址，不允许本机、内网或保留地址')
    ips.sort(key=lambda ip: ipaddress.ip_address(ip).version)
    return u, host, port, ips


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self.pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)


class PinnedHTTPS(PinnedHTTP):
    def connect(self):
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def fetch(url,metadata=None,user_agent='Auto-Argo-Node/1.0'):
    deadline = time.monotonic() + 25
    try:
        for redirect in range(4):
            u, host, port, ips = public_target(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('拉取订阅超时')
            klass = PinnedHTTPS if u.scheme == 'https' else PinnedHTTP
            connection = klass(host, port, ips[0], min(10, remaining))
            try:
                # HTTPConnection builds Host from the original hostname; connect uses only the validated IP.
                path = quote(u.path or '/', safe="/%:@!$&'()*+,;=-._~")
                if u.query:
                    path += '?' + quote(u.query, safe="=&%/:?@!$'()*+,;~-._")
                connection.request('GET', path, headers={'User-Agent': user_agent, 'Accept-Encoding': 'identity', 'Accept': 'text/plain,application/json,application/yaml,*/*'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if redirect == 3 or not location:
                        raise ValueError('订阅 URL 重定向次数过多或无效')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError('订阅服务器返回 HTTP ' + str(response.status))
                length = response.getheader('Content-Length')
                if length and (not length.isdigit() or int(length) > MAX_BYTES):
                    raise ValueError('订阅内容超过 1 MiB 限制')
                chunks, size = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError('拉取订阅超时')
                    if connection.sock:
                        connection.sock.settimeout(min(10, remaining))
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk); size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError('订阅内容超过 1 MiB 限制')
                content = b''.join(chunks)
                encoding = (response.getheader('Content-Encoding') or '').lower()
                if encoding == 'gzip':
                    with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                        content = stream.read(MAX_BYTES + 1)
                elif encoding not in ('', 'identity'):
                    raise ValueError('订阅使用了不支持的压缩格式')
                if len(content) > MAX_BYTES:
                    raise ValueError('订阅内容超过 1 MiB 限制')
                if metadata is not None:
                    metadata['subscription_userinfo']=response.getheader('Subscription-Userinfo') or ''
                return content.decode('utf-8-sig')
            finally:
                connection.close()
    except (OSError, UnicodeError, http.client.HTTPException, EOFError, zlib.error):
        # Never include upstream URLs, paths, auth tokens, or response bodies in error messages.
        raise ValueError('无法读取订阅，请检查地址、网络及服务器证书')
    except ValueError:
        raise
    raise ValueError('无法读取订阅')
