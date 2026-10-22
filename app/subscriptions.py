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

from . import db, external_nodes, proxy_chains, telemetry, regions
from .node_formats import parse_uri, clash_proxy, singbox_outbound, to_uri

Format = Literal['base64', 'raw', 'clash', 'singbox']
FORMATS = ('base64', 'raw', 'clash', 'singbox')


def init():
    external_nodes.init()
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
    CREATE TABLE IF NOT EXISTS subscription_external_nodes (
        subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        node_id INTEGER NOT NULL REFERENCES external_nodes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, PRIMARY KEY(subscription_id,node_id)
    );
    ''')
    c=db.get_conn()
    columns={r['name'] for r in c.execute('PRAGMA table_info(subscriptions)')}
    if 'chains_json' not in columns:c.execute("ALTER TABLE subscriptions ADD COLUMN chains_json TEXT NOT NULL DEFAULT '[]'")
    if 'chain_only' not in columns:c.execute('ALTER TABLE subscriptions ADD COLUMN chain_only INTEGER NOT NULL DEFAULT 0')
    if 'traffic_limit_bytes' not in columns:c.execute('ALTER TABLE subscriptions ADD COLUMN traffic_limit_bytes INTEGER NOT NULL DEFAULT 0')
    c.commit()


class SubscriptionBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    node_ids: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list, max_length=1000)
    external_node_ids: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list, max_length=1000)
    chains: list[proxy_chains.Chain]=Field(default_factory=list,max_length=30)
    chain_only: bool=False
    traffic_limit_bytes: int=Field(default=0,ge=0,le=9_000_000_000_000_000)
    enabled: bool = True
    expires_at: str = Field(default='', max_length=40)

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('请输入有效的订阅名称')
        return value

    @field_validator('node_ids', 'external_node_ids')
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


def external_members(subscription_id):
    return [dict(r) for r in db.get_conn().execute('''SELECT n.* FROM subscription_external_nodes m
        JOIN external_nodes n ON n.id=m.node_id WHERE m.subscription_id=? ORDER BY m.position''', (subscription_id,))]


def eligible(node):
    return bool(node['share_link']) and node['status'] not in ('removed', 'uninstalling')


def token_for(subscription, secret):
    msg = f"subscription:{subscription['id']}:{subscription['token_version']}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def expired(subscription):
    return bool(subscription['expires_at'] and subscription['expires_at'] <= db.utcnow())


def describe(subscription, public_url, secret):
    members = member_nodes(subscription['id'])
    imported = external_members(subscription['id'])
    result = {k: v for k, v in subscription.items() if k != 'token_version'}
    result.update(enabled=bool(subscription['enabled']), expired=expired(subscription),
                  node_ids=[n['id'] for n in members], external_node_ids=[n['id'] for n in imported],
                  node_count=len(members)+len(imported), managed_node_count=len(members), external_node_count=len(imported),
                  exportable_count=sum(eligible(n) for n in members)+sum(bool(n['enabled']) for n in imported))
    base = f"{public_url()}/subscribe/{subscription['id']}/{token_for(subscription, secret)}"
    chains=proxy_chains.configured(subscription)
    available={('managed',n['id']) for n in members if eligible(n)}|{('external',n['id']) for n in imported if n['enabled']}
    errors=proxy_chains.errors(chains,available)
    result.pop('chains_json',None)
    result.update(chains=chains,chain_only=bool(subscription.get('chain_only')),chain_count=len(chains),chain_errors=errors)
    if chains:
        result['exportable_count']=0 if errors else len(chains)+(0 if result['chain_only'] else result['exportable_count'])
    result['usage']=usage(subscription,members,imported)
    result['formats']=['clash','singbox'] if chains else list(FORMATS)
    result['urls'] = {fmt: base + '?format=' + fmt for fmt in result['formats']}
    return result


def save(body, subscription_id=None):
    c = db.get_conn()
    with c:
        existing = set()
        existing_external = set()
        if len(body.node_ids)+len(body.external_node_ids)>1000:
            raise HTTPException(422, '一个订阅最多选择 1000 个节点')
        if subscription_id is not None:
            previous=get(subscription_id)
            if 'traffic_limit_bytes' not in body.model_fields_set:
                body=body.model_copy(update={'traffic_limit_bytes':previous.get('traffic_limit_bytes',0)})
            if 'chains' not in body.model_fields_set:
                body=body.model_copy(update={'chains':[proxy_chains.Chain.model_validate(chain) for chain in proxy_chains.configured(previous)]})
            if 'chain_only' not in body.model_fields_set:
                body=body.model_copy(update={'chain_only':bool(previous.get('chain_only'))})
            existing = {n['id'] for n in member_nodes(subscription_id)}
            existing_external = {n['id'] for n in external_members(subscription_id)}
        # Fail atomically if the selection contains a missing or uninstalled node.
        for node_id in body.node_ids:
            node = db.get_node(node_id)
            if not node or (not eligible(node) and node_id not in existing):
                raise HTTPException(409, f'节点 #{node_id} 已删除、正在卸载或尚未部署，请重新选择')
        for node_id in body.external_node_ids:
            imported = external_nodes.get(node_id)
            if not imported['enabled'] and node_id not in existing_external:
                raise HTTPException(409, '所选外部节点已停用，请重新选择')
        proxy_chains.validate(body.chains,body.node_ids,body.external_node_ids,body.chain_only)
        now = db.utcnow()
        if subscription_id is None:
            cur = c.execute('''INSERT INTO subscriptions(name,enabled,expires_at,token_version,created_at,updated_at)
                VALUES (?,?,?,?,?,?)''', (body.name, int(body.enabled), body.expires_at, secrets.token_hex(32), now, now))
            subscription_id = cur.lastrowid
        else:
            c.execute('UPDATE subscriptions SET name=?,enabled=?,expires_at=?,updated_at=? WHERE id=?',
                      (body.name, int(body.enabled), body.expires_at, now, subscription_id))
            c.execute('DELETE FROM subscription_nodes WHERE subscription_id=?', (subscription_id,))
            c.execute('DELETE FROM subscription_external_nodes WHERE subscription_id=?', (subscription_id,))
        c.execute('UPDATE subscriptions SET chains_json=?,chain_only=? WHERE id=?',(json.dumps([chain.model_dump() for chain in body.chains],ensure_ascii=False),int(body.chain_only),subscription_id))
        c.execute('UPDATE subscriptions SET traffic_limit_bytes=? WHERE id=?',(body.traffic_limit_bytes,subscription_id))
        c.executemany('INSERT INTO subscription_nodes VALUES (?,?,?)',
                      [(subscription_id, node_id, pos) for pos, node_id in enumerate(body.node_ids)])
        c.executemany('INSERT INTO subscription_external_nodes VALUES (?,?,?)',
                      [(subscription_id, node_id, pos) for pos, node_id in enumerate(body.external_node_ids)])
    return get(subscription_id)


def usage(subscription,members=None,imported=None):
    members=member_nodes(subscription['id']) if members is None else members
    imported=external_members(subscription['id']) if imported is None else imported
    chains=proxy_chains.configured(subscription)
    used=None
    if subscription.get('chain_only') and chains:
        used={(h['kind'],h['id']) for chain in chains for h in chain['hops']}
    ids={n['id'] for n in members if eligible(n) and (used is None or ('managed',n['id']) in used)}
    external=sum(bool(n['enabled']) and (used is None or ('external',n['id']) in used) for n in imported)
    upload=download=measured=stale=0;since=[];now=int(datetime.now(timezone.utc).timestamp())
    c=db.get_conn()
    if ids and c.execute("SELECT 1 FROM sqlite_master WHERE name='traffic_totals'").fetchone():
        rows=c.execute('SELECT t.*,s.sampled_at FROM traffic_totals t LEFT JOIN traffic_samples s ON s.node_id=t.node_id WHERE t.node_id IN ('+','.join('?' for _ in ids)+')',tuple(sorted(ids))).fetchall()
        for row in rows:
            upload+=row['rx'];download+=row['tx'];measured+=1
            stale+=int(not row['sampled_at'] or now-row['sampled_at']>=180)
            if row['since']:since.append(row['since'])
    return {'upload':upload,'download':download,'used':upload+download,'total':subscription.get('traffic_limit_bytes',0),
      'scope':'shared-node-port-aggregate','managed_count':len(ids),'measured_count':measured,'external_count':external,
      'unmeasured_count':len(ids)-measured+external,'stale_count':stale,'complete':measured==len(ids) and not external and bool(ids),
      'since':min(since) if since else '', 'note':'所选自建节点自启用采集以来的共享总量，包含其他订阅/连接；外部节点未计入，链路节点分别计数。额度仅作展示，不限制服务器转发。'}


def usage_headers(subscription):
    u=usage(subscription);headers={'X-Traffic-Scope':u['scope'],'X-Traffic-Coverage':'complete' if u['complete'] else 'partial'}
    values=[]
    if u['measured_count']:
        values=[f"upload={u['upload']}",f"download={u['download']}",f"total={u['total']}"]
    if subscription.get('expires_at'):
        values.append('expire='+str(int(datetime.fromisoformat(subscription['expires_at'].replace('Z','+00:00')).timestamp())))
    if values:headers['Subscription-Userinfo']='; '.join(values)
    return headers


def parse_node(node, name):
    return parse_uri(node['share_link'], name)


def render(subscription, fmt):
    members = [n for n in member_nodes(subscription['id']) if eligible(n)]
    members += [dict(n, share_link=n['uri'], remark=n['name'], source_kind='external')
                for n in external_members(subscription['id']) if n['enabled']]
    chains=proxy_chains.configured(subscription)
    if chains and fmt in ('raw','base64'):raise HTTPException(409,'链式代理仅支持 Clash/Mihomo 或 sing-box 完整配置，普通节点链接和 Base64 无法保留链路关系')
    available={('external' if n.get('source_kind')=='external' else 'managed',n['id']) for n in members}
    problems=proxy_chains.errors(chains,available)
    if problems:raise HTTPException(409,problems[0])
    if not members:
        raise HTTPException(409, '订阅中暂无可导出节点')
    used = {'DIRECT', '节点选择', 'direct', 'proxy'}
    parsed, links = [], []
    parsed_by_ref={}
    for n in members:
        name = regions.enrich(n,'external' if n.get('source_kind')=='external' else 'managed')['display_name'][:80] or f"节点 {n['id']}"
        if name in used:
            suffix=f" [{'外部' if n.get('source_kind') == 'external' else ''}{n['id']}]"
            name=name[:80-len(suffix)]+suffix
        base_name=name;counter=1
        while name in used:
            suffix='_'+str(counter);name=base_name[:80-len(suffix)]+suffix;counter+=1
        used.add(name)
        try:
            parsed.append(parse_node(n, name))
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, f"节点 #{n['id']} 无法导出：{exc}")
        parsed_by_ref[('external' if n.get('source_kind')=='external' else 'managed',n['id'])]=parsed[-1]
        links.append(to_uri(parsed[-1]))
    if fmt in ('raw', 'base64'):
        text = '\n'.join(links) + '\n'
        if fmt == 'base64':
            text = base64.b64encode(text.encode()).decode() + '\n'
        return text, 'text/plain', 'txt', len(parsed)
    expanded,names=proxy_chains.expand(chains,parsed_by_ref,not bool(subscription.get('chain_only')))
    count=len(names)
    choices=names+([] if chains and subscription.get('chain_only') else ['DIRECT'])
    if fmt == 'clash':
        data = {'mixed-port': 7890, 'allow-lan': False, 'mode': 'rule', 'log-level': 'info',
                'proxies': proxy_chains.proxies(expanded,fmt),
                'proxy-groups': [{'name': '节点选择', 'type': 'select', 'proxies': choices}],
                'rules': ['MATCH,节点选择']}
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False), 'text/yaml', 'yaml', count
    data = {'log': {'level': 'info'},
            'dns': {'servers': [{'type': 'https', 'tag': 'dns-direct', 'server': '1.1.1.1'}]},
            'inbounds': [{'type': 'mixed', 'tag': 'mixed-in', 'listen': '127.0.0.1', 'listen_port': 7890}],
            'outbounds': [{'type': 'selector', 'tag': 'proxy', 'outbounds': names+([] if chains and subscription.get('chain_only') else ['direct']), 'default': names[0]}]
                         + proxy_chains.proxies(expanded,fmt) + [{'type': 'direct', 'tag': 'direct'}],
            'route': {'final': 'proxy', 'auto_detect_interface': True,
                      'default_domain_resolver': {'server': 'dns-direct', 'strategy': 'prefer_ipv4'}}}
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n', 'application/json', 'json', count


def register(app, require_login, public_url, secret):
    def owner(request):
        require_login(request)
        if request.headers.get('origin') and urlsplit(request.headers['origin']).netloc != request.headers.get('host'):
            raise HTTPException(403, '请求来源不匹配')

    external_nodes.register(app, owner)

    def response(subscription, fmt, download=False):
        text, media, ext, count = render(subscription, fmt)
        disposition = 'attachment' if download else 'inline'
        return Response(text, media_type=media, headers={
            **usage_headers(subscription), 'Cache-Control': 'no-store', 'profile-update-interval': '1',
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
