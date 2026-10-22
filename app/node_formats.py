"""Validated common proxy formats; independent of management and networking."""
import base64
import binascii
import ipaddress
import json
import re
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
from uuid import UUID

PROTOCOLS = ('vless', 'vmess', 'trojan', 'ss', 'hysteria2', 'tuic')
CIPHERS = {'aes-128-gcm', 'aes-192-gcm', 'aes-256-gcm', 'chacha20-ietf-poly1305',
           'xchacha20-ietf-poly1305', '2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm',
           '2022-blake3-chacha20-poly1305', 'aes-128-ctr', 'aes-192-ctr', 'aes-256-ctr',
           'aes-128-cfb', 'aes-192-cfb', 'aes-256-cfb', 'chacha20-ietf'}


def b64decode(value):
    try:
        raw = re.sub(r'\s+', '', value).encode('ascii')
        return base64.b64decode(raw + b'=' * (-len(raw) % 4), altchars=b'-_', validate=True).decode('utf-8-sig')
    except (UnicodeError, ValueError, binascii.Error):
        raise ValueError('Base64 内容无效')


def b64encode(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip('=')


def text(value, label, limit=4096, allow_empty=True):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(label + '格式无效')
    if not allow_empty and not value:
        raise ValueError(label + '不能为空')
    return value


def display_name(value, fallback):
    if value is not None and not isinstance(value, str):
        raise ValueError('节点名称必须为文本')
    return ' '.join(str(value or fallback).split())[:80] or fallback[:80]


def boolean(value):
    if value in (True, 1, '1', 'true'):
        return True
    if value in (False, 0, '0', 'false', '', None):
        return False
    raise ValueError('布尔参数无效')


def validate(p):
    p = dict(p)
    kind = p.get('protocol')
    if kind not in PROTOCOLS:
        raise ValueError('暂不支持该协议')
    server = text(p.get('server'), '服务器地址', 253, False)
    try:
        server = str(ipaddress.ip_address(server))
    except ValueError:
        try:
            server = server.encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise ValueError('服务器地址无效')
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', server) or server.startswith('.'):
            raise ValueError('服务器地址无效')
    p['server'] = server
    try:
        p['port'] = int(p['port'])
        if not 1 <= p['port'] <= 65535:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError('端口必须在 1–65535 之间')
    p['name'] = display_name(p.get('name'), f'{kind} {server}')
    for key, default in dict(uuid='', password='', cipher='', security='none', transport='tcp',
                             sni=server, host=server, path='/', fingerprint='chrome', flow='',
                             public_key='', short_id='', service_name='', obfs='', obfs_password='',
                             congestion='cubic', udp_relay_mode='native').items():
        p[key] = text(p.get(key, default), key)
    p['alpn'] = p.get('alpn') or []
    if not isinstance(p['alpn'], list) or len(p['alpn']) > 10:
        raise ValueError('ALPN 格式无效')
    p['alpn'] = [text(x, 'ALPN', 100, False) for x in p['alpn']]
    p['insecure'] = boolean(p.get('insecure', False))
    for key in ('early_data', 'alter_id'):
        try:
            p[key] = int(p.get(key, 0))
            if not 0 <= p[key] <= 65535:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError(key + ' 参数无效')
    if kind in ('vless', 'vmess', 'tuic'):
        try:
            p['uuid'] = str(UUID(p['uuid']))
        except ValueError:
            raise ValueError('UUID 无效')
    if kind in ('trojan', 'ss', 'hysteria2', 'tuic') and not p['password']:
        raise ValueError('节点密码不能为空')
    if p['transport'] not in ('tcp', 'ws', 'grpc'):
        raise ValueError('目前支持 TCP、WebSocket 和 gRPC 传输')
    if p['transport'] != 'ws':
        p['host'] = server
        p['path'] = '/'
        p['early_data'] = 0
    if p['fingerprint'] not in ('', 'chrome', 'firefox', 'edge', 'safari', '360', 'qq', 'ios', 'android', 'random', 'randomized'):
        raise ValueError('暂不支持该 TLS 指纹')
    if p['security'] not in ('none', 'tls', 'reality'):
        raise ValueError('不支持的 TLS 类型')
    if p['security'] == 'reality':
        if kind != 'vless' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', p['public_key']):
            raise ValueError('Reality 公钥或协议无效')
        if not re.fullmatch(r'[a-fA-F0-9]{0,16}', p['short_id']) or len(p['short_id']) % 2:
            raise ValueError('Reality short-id 无效')
    if p['flow'] and (kind != 'vless' or p['flow'] != 'xtls-rprx-vision' or p['transport'] != 'tcp'):
        raise ValueError('不支持的 flow 组合')
    if kind == 'vmess' and p['cipher'] not in ('auto', 'none', 'zero', 'aes-128-gcm', 'chacha20-poly1305', 'aes-128-ctr'):
        raise ValueError('不支持的 VMess 加密方式')
    if kind == 'ss':
        if p['cipher'] not in CIPHERS:
            raise ValueError('不支持的 Shadowsocks 加密方式')
        if p['cipher'].startswith('2022-'):
            required = 16 if 'aes-128' in p['cipher'] else 32
            try:
                if len(base64.b64decode(p['password'], validate=True)) != required:
                    raise ValueError()
            except (ValueError, binascii.Error):
                raise ValueError('Shadowsocks 2022 密钥长度无效')
    if kind in ('hysteria2', 'tuic'):
        p['security'] = 'tls'
        if not p['alpn']:
            p['alpn'] = ['h3']
        if p['transport'] != 'tcp':
            raise ValueError('该 QUIC 协议不能附加 WS/gRPC 传输')
    if p['obfs'] not in ('', 'salamander') or (p['obfs'] and not p['obfs_password']):
        raise ValueError('Hysteria2 混淆参数无效')
    if p['congestion'] not in ('cubic', 'bbr', 'new_reno') or p['udp_relay_mode'] not in ('native', 'quic'):
        raise ValueError('TUIC 参数无效')
    return p


def parse_uri(uri, name=None):
    uri = text(uri.strip(), '节点链接', 16384, False)
    if uri.startswith('vmess://'):
        try:
            v = json.loads(b64decode(uri[8:].split('#', 1)[0]))
            if not isinstance(v, dict):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise ValueError('VMess 链接必须为 Base64 JSON 格式')
        if v.get('type', 'none') not in ('none', ''):
            raise ValueError('暂不支持 VMess TCP HTTP 伪装')
        p = dict(protocol='vmess', name=name or v.get('ps'), server=v.get('add'), port=v.get('port'),
                 uuid=v.get('id', ''), alter_id=v.get('aid', 0), cipher=v.get('scy', 'auto'),
                 transport=v.get('net', 'tcp'), security='tls' if v.get('tls') == 'tls' else 'none',
                 sni=v.get('sni') or v.get('host') or v.get('add'), host=v.get('host') or v.get('add'),
                 path=v.get('path') or '/', fingerprint=v.get('fp') or 'chrome',
                 alpn=str(v.get('alpn') or '').split(',') if v.get('alpn') else [],
                 insecure=boolean(v.get('allowInsecure', False)))
        if p['transport'] == 'grpc':
            p['service_name'] = v.get('path') or ''
    else:
        # Legacy SS embeds the entire method/password/endpoint in one Base64 value.
        if uri.startswith('ss://') and '@' not in uri.split('#', 1)[0]:
            payload, _, fragment = uri[5:].partition('#')
            uri = 'ss://' + b64decode(payload) + ('#' + fragment if fragment else '')
        try:
            u = urlsplit(uri)
            kind = 'hysteria2' if u.scheme == 'hy2' else u.scheme
            server, port = u.hostname, u.port
            if port is None and kind in ('vless', 'trojan', 'hysteria2', 'tuic'):
                port = 443
            q = parse_qs(u.query, keep_blank_values=True)
        except ValueError:
            raise ValueError('节点地址或端口无效')
        value = lambda key, default='': q.get(key, [default])[0]
        allowed = {'encryption', 'security', 'sni', 'peer', 'type', 'host', 'path', 'ed', 'fp', 'flow',
                   'pbk', 'sid', 'spx', 'alpn', 'allowInsecure', 'insecure', 'serviceName', 'service_name',
                   'headerType', 'mode', 'obfs', 'obfs-password', 'congestion_control', 'udp_relay_mode'}
        if set(q) - allowed:
            raise ValueError('链接包含暂不支持的附加参数')
        if value('mode') not in ('', 'gun'):
            raise ValueError('暂不支持该 gRPC 模式')
        if value('encryption', 'none') not in ('', 'none') or value('headerType', 'none') not in ('none', ''):
            raise ValueError('暂不支持该加密或 TCP 伪装参数')
        default_security = 'tls' if kind in ('trojan', 'hysteria2', 'tuic') else 'none'
        p = dict(protocol=kind, name=name or unquote(u.fragment), server=server, port=port,
                 security=value('security', default_security), transport=value('type', 'tcp'),
                 sni=value('sni', value('peer', server)), host=value('host', value('sni', server)),
                 path=value('path', '/'), fingerprint=value('fp', 'chrome'), flow=value('flow'),
                 public_key=value('pbk'), short_id=value('sid'),
                 alpn=value('alpn').split(',') if value('alpn') else [],
                 insecure=boolean(value('allowInsecure', value('insecure', '0')).lower()),
                 service_name=value('serviceName', value('service_name')), early_data=value('ed', '0'),
                 obfs=value('obfs'), obfs_password=value('obfs-password'),
                 congestion=value('congestion_control', 'cubic'), udp_relay_mode=value('udp_relay_mode', 'native'))
        user = unquote(u.username or '')
        if kind in ('vless', 'tuic'):
            p['uuid'] = user
        if kind in ('trojan', 'hysteria2'):
            p['password'] = user + (':' + unquote(u.password) if u.password is not None else '')
        if kind == 'tuic':
            p['password'] = unquote(u.password or '')
        if kind == 'ss':
            auth = user + ':' + unquote(u.password) if u.password is not None else b64decode(user)
            p['cipher'], sep, p['password'] = auth.partition(':')
            if not sep:
                raise ValueError('Shadowsocks 认证格式无效')
    # Common clients encode WS Early Data in the path query.
    if p.get('transport') == 'ws':
        parts = urlsplit(str(p.get('path', '/')))
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        p['early_data'] = next((v for k, v in pairs if k == 'ed'), p.get('early_data', 0))
        p['path'] = urlunsplit(('', '', parts.path or '/', urlencode([(k, v) for k, v in pairs if k != 'ed']), ''))
    return validate(p)


def to_uri(p):
    p = validate(p)
    kind = p['protocol']
    host = '[' + p['server'] + ']' if ':' in p['server'] else p['server']
    endpoint = host + ':' + str(p['port'])
    path = p['path']
    if p['transport'] == 'ws' and p['early_data']:
        path += ('&' if '?' in path else '?') + 'ed=' + str(p['early_data'])
    if kind == 'vmess':
        v = dict(v='2', ps=p['name'], add=p['server'], port=str(p['port']), id=p['uuid'],
                 aid=str(p['alter_id']), scy=p['cipher'], net=p['transport'], type='none',
                 host=p['host'], path=p['service_name'] if p['transport'] == 'grpc' else path,
                 tls='tls' if p['security'] == 'tls' else '', sni=p['sni'], fp=p['fingerprint'],
                 alpn=','.join(p['alpn']), allowInsecure=p['insecure'])
        return 'vmess://' + base64.b64encode(json.dumps(v, ensure_ascii=False, separators=(',', ':')).encode()).decode()
    if kind == 'ss':
        return 'ss://' + b64encode(p['cipher'] + ':' + p['password']) + '@' + endpoint + '#' + quote(p['name'], safe='')
    auth = p['uuid'] if kind == 'vless' else p['password']
    if kind == 'tuic':
        auth = quote(p['uuid'], safe='') + ':' + quote(p['password'], safe='')
    else:
        auth = quote(auth, safe='')
    q = {}
    if kind in ('vless', 'trojan'):
        q.update(security=p['security'], type=p['transport'])
        if p['security'] != 'none':
            q.update(sni=p['sni'], fp=p['fingerprint'])
        if p['transport'] == 'ws':
            q.update(host=p['host'], path=path)
        if p['transport'] == 'grpc':
            q['serviceName'] = p['service_name']
        if p['flow']:
            q['flow'] = p['flow']
        if p['security'] == 'reality':
            q.update(pbk=p['public_key'], sid=p['short_id'])
    else:
        q['sni'] = p['sni']
        if kind == 'hysteria2' and p['obfs']:
            q.update(obfs=p['obfs'], **{'obfs-password': p['obfs_password']})
        if kind == 'tuic':
            q.update(congestion_control=p['congestion'], udp_relay_mode=p['udp_relay_mode'])
    if p['alpn']:
        q['alpn'] = ','.join(p['alpn'])
    if p['insecure']:
        q['insecure'] = '1'
    return kind + '://' + auth + '@' + endpoint + ('?' + urlencode(q) if q else '') + '#' + quote(p['name'], safe='')


def clash_proxy(p):
    kind = p['protocol']
    out = dict(name=p['name'], type=kind, server=p['server'], port=p['port'], udp=True)
    if kind in ('vless', 'vmess', 'tuic'):
        out['uuid'] = p['uuid']
    if kind == 'vmess':
        out.update(alterId=p['alter_id'], cipher=p['cipher'])
    if kind in ('trojan', 'ss', 'hysteria2', 'tuic'):
        out['password'] = p['password']
    if kind == 'ss':
        out['cipher'] = p['cipher']
        return out
    if p['flow']:
        out['flow'] = p['flow']
    if p['security'] != 'none':
        out.update(servername=p['sni'], **{'client-fingerprint': p['fingerprint'], 'skip-cert-verify': p['insecure']})
        if kind in ('vless', 'vmess'):
            out['tls'] = True
        if p['alpn']:
            out['alpn'] = p['alpn']
    if p['security'] == 'reality':
        out['reality-opts'] = {'public-key': p['public_key'], 'short-id': p['short_id']}
    if p['transport'] == 'ws':
        out['network'] = 'ws'
        out['ws-opts'] = {'path': p['path'], 'headers': {'Host': p['host']}}
        if p['early_data']:
            out['ws-opts'].update({'max-early-data': p['early_data'], 'early-data-header-name': 'Sec-WebSocket-Protocol'})
    if p['transport'] == 'grpc':
        out.update(network='grpc', **{'grpc-opts': {'grpc-service-name': p['service_name']}})
    if kind == 'hysteria2' and p['obfs']:
        out.update(obfs=p['obfs'], **{'obfs-password': p['obfs_password']})
    if kind == 'tuic':
        out.update({'congestion-controller': p['congestion'], 'udp-relay-mode': p['udp_relay_mode']})
    return out


def singbox_outbound(p):
    kind = p['protocol']
    out = dict(type='shadowsocks' if kind == 'ss' else kind, tag=p['name'], server=p['server'], server_port=p['port'])
    if kind in ('vless', 'vmess', 'tuic'):
        out['uuid'] = p['uuid']
    if kind == 'vmess':
        out.update(security=p['cipher'], alter_id=p['alter_id'])
    if kind in ('trojan', 'ss', 'hysteria2', 'tuic'):
        out['password'] = p['password']
    if kind == 'ss':
        out['method'] = p['cipher']
        return out
    if p['flow']:
        out['flow'] = p['flow']
    if p['security'] != 'none':
        tls = dict(enabled=True, server_name=p['sni'], insecure=p['insecure'])
        if p['fingerprint'] and kind not in ('hysteria2', 'tuic'):
            tls['utls'] = dict(enabled=True, fingerprint=p['fingerprint'])
        if p['alpn']:
            tls['alpn'] = p['alpn']
        if p['security'] == 'reality':
            tls['reality'] = dict(enabled=True, public_key=p['public_key'], short_id=p['short_id'])
        out['tls'] = tls
    if p['transport'] == 'ws':
        out['transport'] = dict(type='ws', path=p['path'], headers={'Host': p['host']})
        if p['early_data']:
            out['transport'].update(max_early_data=p['early_data'], early_data_header_name='Sec-WebSocket-Protocol')
    if p['transport'] == 'grpc':
        out['transport'] = dict(type='grpc', service_name=p['service_name'])
    if kind == 'hysteria2' and p['obfs']:
        out['obfs'] = dict(type=p['obfs'], password=p['obfs_password'])
    if kind == 'tuic':
        out.update(congestion_control=p['congestion'], udp_relay_mode=p['udp_relay_mode'])
    return out


def scalar_string(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError('节点参数必须为文本或数字')
    return str(value)


def from_config(node, flavor):
    if not isinstance(node, dict):
        raise ValueError('节点配置必须是对象')
    kind = node.get('type')
    kind = 'ss' if kind == 'shadowsocks' else kind
    if kind not in PROTOCOLS:
        raise ValueError('暂不支持该协议')
    # Do not silently strip chains, plugins, pins, or nonrepresentable transports.
    dangerous = ('dialer-proxy', 'detour', 'plugin', 'plugin-opts', 'plugin_opts', 'smux', 'multiplex',
                 'reality-public-key', 'certificate', 'certificate_path', 'up', 'down', 'up_mbps', 'down_mbps',
                 'ports', 'server_ports', 'port-hopping', 'client-fingerprint-cert', 'fingerprint',
                 'servername-fingerprint', 'tls-fingerprint', 'packet-encoding', 'packet_encoding')
    if any(node.get(k) for k in dangerous):
        raise ValueError('节点包含暂不支持的链式代理、插件或高级参数')
    if flavor == 'clash':
        reality = node.get('reality-opts') or {}
        p = dict(protocol=kind, name=node.get('name'), server=node.get('server'), port=node.get('port'),
                 uuid=node.get('uuid', ''), password=scalar_string(node.get('password', '')), cipher=node.get('cipher', 'auto' if kind == 'vmess' else ''),
                 alter_id=node.get('alterId', 0), security='reality' if reality else ('tls' if boolean(node.get('tls', False)) or kind in ('trojan', 'hysteria2', 'tuic') else 'none'),
                 transport=node.get('network', 'tcp'), sni=node.get('servername') or node.get('sni') or node.get('server'),
                 fingerprint=node.get('client-fingerprint', 'chrome'), insecure=node.get('skip-cert-verify', False),
                 alpn=node.get('alpn', []), flow=node.get('flow', ''), public_key=reality.get('public-key', ''), short_id=scalar_string(reality.get('short-id', '')),
                 congestion=node.get('congestion-controller', 'cubic'), udp_relay_mode=node.get('udp-relay-mode', 'native'),
                 obfs=node.get('obfs', ''), obfs_password=node.get('obfs-password', ''))
        ws = node.get('ws-opts') or {}
        headers = ws.get('headers') or {}
        if set(headers) - {'Host', 'host'} or ws.get('early-data-header-name', 'Sec-WebSocket-Protocol') != 'Sec-WebSocket-Protocol':
            raise ValueError('暂不支持该 WS 请求头配置')
        p.update(host=headers.get('Host') or headers.get('host') or p['sni'], path=ws.get('path', '/'),
                 early_data=ws.get('max-early-data', 0), service_name=(node.get('grpc-opts') or {}).get('grpc-service-name', ''))
    else:
        tls = node.get('tls') or {}
        if any(tls.get(k) for k in ('certificate', 'certificate_path', 'ech', 'client_certificate', 'client_key', 'disable_sni')):
            raise ValueError('暂不支持该高级 TLS 配置')
        reality = tls.get('reality') or {}
        transport = node.get('transport') or {}
        p = dict(protocol=kind, name=node.get('tag'), server=node.get('server'), port=node.get('server_port'),
                 uuid=node.get('uuid', ''), password=scalar_string(node.get('password', '')), cipher=node.get('method') if kind == 'ss' else node.get('security', 'auto'),
                 alter_id=node.get('alter_id', 0), security='reality' if boolean(reality.get('enabled', False)) else ('tls' if boolean(tls.get('enabled', False)) else 'none'),
                 transport=transport.get('type', 'tcp'), sni=tls.get('server_name') or node.get('server'),
                 fingerprint=(tls.get('utls') or {}).get('fingerprint', 'chrome'), insecure=tls.get('insecure', False),
                 alpn=tls.get('alpn', []), flow=node.get('flow', ''), public_key=reality.get('public_key', ''), short_id=scalar_string(reality.get('short_id', '')),
                 congestion=node.get('congestion_control', 'cubic'), udp_relay_mode=node.get('udp_relay_mode', 'native'),
                 obfs=(node.get('obfs') or {}).get('type', ''), obfs_password=(node.get('obfs') or {}).get('password', ''))
        headers = transport.get('headers') or {}
        if set(headers) - {'Host', 'host'} or transport.get('early_data_header_name', 'Sec-WebSocket-Protocol') != 'Sec-WebSocket-Protocol':
            raise ValueError('暂不支持该 WS 请求头配置')
        p.update(host=headers.get('Host') or headers.get('host') or p['sni'], path=transport.get('path', '/'),
                 early_data=transport.get('max_early_data', 0), service_name=transport.get('service_name', ''))
    return validate(p)
