"""Selective, live subscriptions. No external conversion service or URL fetching."""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import UUID

import yaml
from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from . import db

Format = Literal['base64', 'raw', 'clash', 'singbox']
FORMATS = ('base64', 'raw', 'clash', 'singbox')


def init():
    db.get_conn().executescript('''
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, expires_at TEXT NOT NULL DEFAULT '',
        token_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS subscription_nodes (
        subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        PRIMARY KEY(subscription_id,node_id)
    );
    ''')
    db.get_conn().commit()


class SubscriptionBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    node_ids: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list, max_length=1000)
    enabled: bool = True
    expires_at: str = Field(default='', max_length=40)

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('请输入有效的订阅名称')
        return value

    @field_validator('node_ids')
    @classmethod
    def unique_ids(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('节点不能重复')
        return value

    @field_validator('expires_at')
    @classmethod
    def valid_expiry(cls, value):
        if not value:
            return ''
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise ValueError()
            return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            raise ValueError('到期时间必须包含时区')


def get(subscription_id):
    r = db.get_conn().execute('SELECT * FROM subscriptions WHERE id=?', (subscription_id,)).fetchone()
    if not r:
        raise HTTPException(404, '订阅不存在')
    return dict(r)


def member_nodes(subscription_id):
    return [dict(r) for r in db.get_conn().execute('''SELECT n.* FROM subscription_nodes m
        JOIN nodes n ON n.id=m.node_id WHERE m.subscription_id=? ORDER BY m.position''', (subscription_id,))]


def eligible(node):
    return bool(node['share_link']) and node['status'] not in ('removed', 'uninstalling')


def token_for(subscription, secret):
    msg = f"subscription:{subscription['id']}:{subscription['token_version']}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def expired(subscription):
    return bool(subscription['expires_at'] and subscription['expires_at'] <= db.utcnow())


def describe(subscription, public_url, secret):
    members = member_nodes(subscription['id'])
    result = {k: v for k, v in subscription.items() if k != 'token_version'}
    result.update(enabled=bool(subscription['enabled']), expired=expired(subscription),
                  node_ids=[n['id'] for n in members], node_count=len(members),
                  exportable_count=sum(eligible(n) for n in members))
    base = f"{public_url()}/subscribe/{subscription['id']}/{token_for(subscription, secret)}"
    result['urls'] = {fmt: base + '?format=' + fmt for fmt in FORMATS}
    return result


def save(body, subscription_id=None):
    c = db.get_conn()
    with c:
        existing = set()
        if subscription_id is not None:
            get(subscription_id)
            existing = {n['id'] for n in member_nodes(subscription_id)}
        # Fail atomically if the selection contains a missing or uninstalled node.
        for node_id in body.node_ids:
            node = db.get_node(node_id)
            if not node or (not eligible(node) and node_id not in existing):
                raise HTTPException(409, f'节点 #{node_id} 已删除、正在卸载或尚未部署，请重新选择')
        now = db.utcnow()
        if subscription_id is None:
            cur = c.execute('''INSERT INTO subscriptions(name,enabled,expires_at,token_version,created_at,updated_at)
                VALUES (?,?,?,?,?,?)''', (body.name, int(body.enabled), body.expires_at, secrets.token_hex(32), now, now))
            subscription_id = cur.lastrowid
        else:
            c.execute('UPDATE subscriptions SET name=?,enabled=?,expires_at=?,updated_at=? WHERE id=?',
                      (body.name, int(body.enabled), body.expires_at, now, subscription_id))
            c.execute('DELETE FROM subscription_nodes WHERE subscription_id=?', (subscription_id,))
        c.executemany('INSERT INTO subscription_nodes VALUES (?,?,?)',
                      [(subscription_id, node_id, pos) for pos, node_id in enumerate(body.node_ids)])
    return get(subscription_id)


def parse_node(node, name):
    """Only convert the VLESS transports generated by this application."""
    u = urlsplit(node['share_link'])
    q = parse_qs(u.query, keep_blank_values=True)
    value = lambda key, default='': q.get(key, [default])[0]
    if u.scheme != 'vless' or not u.hostname or not u.username:
        raise ValueError('仅支持本项目生成的 VLESS 节点')
    uuid = str(UUID(u.username))
    transport = value('type', 'tcp')
    security = value('security', 'none')
    if transport not in ('tcp', 'ws') or security not in ('none', 'tls', 'reality'):
        raise ValueError('不支持的 VLESS 传输或安全类型')
    path = value('path', '/')
    parts = urlsplit(path)
    path_query = parse_qsl(parts.query, keep_blank_values=True)
    ed = next((v for k, v in path_query if k == 'ed'), value('ed', '0'))
    ed = int(ed)
    if not 0 <= ed <= 65535:
        raise ValueError('Early Data 参数无效')
    path = urlunsplit(('', '', parts.path or '/', urlencode([(k, v) for k, v in path_query if k != 'ed']), ''))
    if security == 'reality' and not value('pbk'):
        raise ValueError('Reality 公钥缺失')
    return dict(name=name, server=u.hostname, port=u.port or 443, uuid=uuid,
                transport=transport, security=security, sni=value('sni', u.hostname),
                host=value('host', value('sni', u.hostname)), path=path, early_data=ed,
                fingerprint=value('fp', 'chrome'), flow=value('flow'), public_key=value('pbk'),
                short_id=value('sid'), insecure=value('allowInsecure', '0') == '1',
                alpn=[x for x in value('alpn').split(',') if x])


def clash_proxy(p):
    out = dict(name=p['name'], type='vless', server=p['server'], port=p['port'], uuid=p['uuid'], udp=True)
    if p['flow']:
        out['flow'] = p['flow']
    if p['security'] != 'none':
        out.update(tls=True, servername=p['sni'], **{'client-fingerprint': p['fingerprint'], 'skip-cert-verify': p['insecure']})
        if p['alpn']:
            out['alpn'] = p['alpn']
    if p['security'] == 'reality':
        out['reality-opts'] = {'public-key': p['public_key'], 'short-id': p['short_id']}
    if p['transport'] == 'ws':
        out['network'] = 'ws'
        out['ws-opts'] = {'path': p['path'], 'headers': {'Host': p['host']}}
        if p['early_data']:
            out['ws-opts'].update({'max-early-data': p['early_data'], 'early-data-header-name': 'Sec-WebSocket-Protocol'})
    return out


def singbox_outbound(p):
    out = dict(type='vless', tag=p['name'], server=p['server'], server_port=p['port'], uuid=p['uuid'])
    if p['flow']:
        out['flow'] = p['flow']
    if p['security'] != 'none':
        tls = dict(enabled=True, server_name=p['sni'], insecure=p['insecure'])
        if p['fingerprint']:
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
    return out


def render(subscription, fmt):
    members = [n for n in member_nodes(subscription['id']) if eligible(n)]
    if not members:
        raise HTTPException(409, '订阅中暂无可导出节点')
    used = {'DIRECT', '节点选择', 'direct', 'proxy'}
    parsed, links = [], []
    for n in members:
        name = n['remark'] or f"节点 {n['id']}"
        if name in used:
            name = f"{name} [{n['id']}]"
        while name in used:
            name += '_'
        used.add(name)
        try:
            parsed.append(parse_node(n, name))
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, f"节点 #{n['id']} 无法导出：{exc}")
        links.append(n['share_link'].split('#', 1)[0] + '#' + quote(name, safe=''))
    if fmt in ('raw', 'base64'):
        text = '\n'.join(links) + '\n'
        if fmt == 'base64':
            text = base64.b64encode(text.encode()).decode() + '\n'
        return text, 'text/plain', 'txt', len(parsed)
    names = [p['name'] for p in parsed]
    if fmt == 'clash':
        data = {'mixed-port': 7890, 'allow-lan': False, 'mode': 'rule', 'log-level': 'info',
                'proxies': [clash_proxy(p) for p in parsed],
                'proxy-groups': [{'name': '节点选择', 'type': 'select', 'proxies': names + ['DIRECT']}],
                'rules': ['MATCH,节点选择']}
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False), 'text/yaml', 'yaml', len(parsed)
    data = {'log': {'level': 'info'},
            'dns': {'servers': [{'type': 'https', 'tag': 'dns-direct', 'server': '1.1.1.1'}]},
            'inbounds': [{'type': 'mixed', 'tag': 'mixed-in', 'listen': '127.0.0.1', 'listen_port': 7890}],
            'outbounds': [{'type': 'selector', 'tag': 'proxy', 'outbounds': names + ['direct'], 'default': names[0]}]
                         + [singbox_outbound(p) for p in parsed] + [{'type': 'direct', 'tag': 'direct'}],
            'route': {'final': 'proxy', 'auto_detect_interface': True,
                      'default_domain_resolver': {'server': 'dns-direct', 'strategy': 'prefer_ipv4'}}}
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n', 'application/json', 'json', len(parsed)


def register(app, require_login, public_url, secret):
    def owner(request):
        require_login(request)
        if request.headers.get('origin') and urlsplit(request.headers['origin']).netloc != request.headers.get('host'):
            raise HTTPException(403, '请求来源不匹配')

    def response(subscription, fmt, download=False):
        text, media, ext, count = render(subscription, fmt)
        disposition = 'attachment' if download else 'inline'
        return Response(text, media_type=media, headers={
            'Cache-Control': 'no-store', 'profile-update-interval': '1',
            'profile-title': 'base64:' + base64.b64encode(subscription['name'].encode()).decode(),
            'Content-Disposition': f'{disposition}; filename="subscription-{subscription["id"]}.{ext}"',
            'X-Subscription-Node-Count': str(count), 'X-Content-Type-Options': 'nosniff',
        })

    @app.get('/api/subscriptions')
    def list_subscriptions(request: Request):
        owner(request)
        return [describe(dict(r), public_url, secret) for r in db.get_conn().execute('SELECT * FROM subscriptions ORDER BY id DESC')]

    @app.post('/api/subscriptions')
    def create_subscription(body: SubscriptionBody, request: Request):
        owner(request)
        return describe(save(body), public_url, secret)

    @app.put('/api/subscriptions/{subscription_id}')
    def update_subscription(subscription_id: int, body: SubscriptionBody, request: Request):
        owner(request)
        return describe(save(body, subscription_id), public_url, secret)

    @app.delete('/api/subscriptions/{subscription_id}')
    def delete_subscription(subscription_id: int, request: Request):
        owner(request)
        get(subscription_id)
        c = db.get_conn()
        c.execute('DELETE FROM subscriptions WHERE id=?', (subscription_id,))
        c.commit()
        return {'ok': True}

    @app.post('/api/subscriptions/{subscription_id}/rotate')
    def rotate_token(subscription_id: int, request: Request):
        owner(request)
        get(subscription_id)
        c = db.get_conn()
        c.execute('UPDATE subscriptions SET token_version=?,updated_at=? WHERE id=?',
                  (secrets.token_hex(32), db.utcnow(), subscription_id))
        c.commit()
        return describe(get(subscription_id), public_url, secret)

    @app.get('/api/subscriptions/{subscription_id}/preview')
    def preview(subscription_id: int, request: Request, format: Format = 'base64'):
        owner(request)
        text, media, ext, count = render(get(subscription_id), format)
        return {'content': text, 'media_type': media, 'filename': f'subscription-{subscription_id}.{ext}', 'node_count': count}

    @app.get('/api/subscriptions/{subscription_id}/export')
    def export(subscription_id: int, request: Request, format: Format = 'base64', download: bool = True):
        owner(request)
        return response(get(subscription_id), format, download)

    @app.get('/subscribe/{subscription_id}/{token}')
    def subscribe(subscription_id: int, token: str, format: Format = 'base64'):
        subscription = get(subscription_id)
        if (len(token) != 64 or any(c not in '0123456789abcdef' for c in token)
                or not subscription['enabled'] or expired(subscription)
                or not hmac.compare_digest(token_for(subscription, secret), token)):
            raise HTTPException(404, '订阅不存在或已失效')
        return response(subscription, format)
