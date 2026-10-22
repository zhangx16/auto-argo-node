"""Panel-owned named tunnels. API and connector credentials are encrypted at rest."""
import base64
import hashlib
import json
import re
import secrets
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from . import db

LOCK = threading.RLock()


class CFError(Exception):
    pass


def init():
    c = db.get_conn()
    c.executescript('''CREATE TABLE IF NOT EXISTS cloudflare_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), account_id TEXT NOT NULL, zone_id TEXT NOT NULL,
        zone_name TEXT NOT NULL, api_token TEXT NOT NULL, verified_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS node_cloudflare (
        node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
        hostname TEXT NOT NULL UNIQUE, tunnel_id TEXT NOT NULL DEFAULT '', dns_id TEXT NOT NULL DEFAULT '',
        tunnel_token TEXT NOT NULL DEFAULT '', route_port INTEGER NOT NULL, applied_port INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'reserved', created_at TEXT NOT NULL
    );''')
    c.commit()


def cipher():
    secret = (db.DATA_DIR / 'secret.key').read_bytes().strip()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'argo-cloudflare-v1:' + secret).digest()))


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    try:
        return cipher().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        raise CFError('Cloudflare 凭据无法解密，请恢复对应 secret.key 或重新配置')


def settings():
    r = db.get_conn().execute('SELECT * FROM cloudflare_settings WHERE id=1').fetchone()
    if not r:
        raise CFError('请先配置 Cloudflare 账户与域名')
    result = dict(r)
    result['api_token'] = decrypt(result['api_token'])
    return result


def status():
    r = db.get_conn().execute('SELECT account_id,zone_id,zone_name,verified_at FROM cloudflare_settings WHERE id=1').fetchone()
    return dict(r, configured=True) if r else {'configured': False}


def binding(node_id):
    if not db.get_conn().execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_cloudflare'").fetchone():
        return None
    r = db.get_conn().execute('SELECT * FROM node_cloudflare WHERE node_id=?', (node_id,)).fetchone()
    return dict(r) if r else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(method, path, body=None, config=None, missing_ok=False):
    cfg = config or settings()
    req = urllib.request.Request('https://api.cloudflare.com/client/v4' + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + cfg['api_token'], 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise CFError('Cloudflare 响应过大')
            data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read(65536))
        except (ValueError, OSError):
            data = {}
        if missing_ok and (exc.code == 404 or any(x.get('code') == 81044 for x in data.get('errors', []))):
            return None
        codes = ','.join(str(x.get('code', '')) for x in data.get('errors', [])[:4])
        raise CFError(f'Cloudflare 请求失败（HTTP {exc.code}，错误码 {codes or "未知"}）')
    except (OSError, ValueError, urllib.error.URLError):
        raise CFError('Cloudflare API 暂不可用，请稍后重试')
    if not data.get('success'):
        raise CFError('Cloudflare 未确认请求成功')
    return data.get('result')


class ConfigBody(BaseModel):
    account_id: str = Field(pattern=r'^[a-fA-F0-9]{32}$')
    zone_id: str = Field(pattern=r'^[a-fA-F0-9]{32}$')
    api_token: SecretStr = Field(min_length=20, max_length=2048)


def configure(body):
    cfg = {'account_id': body.account_id.lower(), 'zone_id': body.zone_id.lower(), 'api_token': body.api_token.get_secret_value()}
    with LOCK:
        old = status()
        count = db.get_conn().execute('SELECT count(*) FROM node_cloudflare').fetchone()[0]
        if count and old.get('configured') and (old['account_id'] != cfg['account_id'] or old['zone_id'] != cfg['zone_id']):
            raise CFError('仍有固定隧道绑定，不能更换账户或 Zone；可更新同一账户的 API Token')
        zone = api('GET', '/zones/' + cfg['zone_id'], config=cfg)
        if zone.get('status') != 'active' or zone.get('account', {}).get('id') != cfg['account_id']:
            raise CFError('Zone 尚未激活或不属于该账户')
        api('GET', f"/accounts/{cfg['account_id']}/cfd_tunnel?is_deleted=false&per_page=1", config=cfg)
        c = db.get_conn()
        c.execute('INSERT OR REPLACE INTO cloudflare_settings VALUES (1,?,?,?,?,?)',
                  (cfg['account_id'], cfg['zone_id'], zone['name'].lower(), encrypt(cfg['api_token']), db.utcnow()))
        c.commit()
        return status()


def valid_hostname(hostname):
    zone = settings()['zone_name']
    hostname = hostname.strip().lower().rstrip('.')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.' + re.escape(zone), hostname):
        raise CFError('固定域名需使用当前 Zone 下的一级子域名，例如 hk01.' + zone)
    return hostname


def patch_binding(node_id, **fields):
    c = db.get_conn()
    c.execute('UPDATE node_cloudflare SET ' + ','.join(k+'=?' for k in fields) + ' WHERE node_id=?', (*fields.values(), node_id))
    c.commit()


def route(node_id, port):
    b = binding(node_id); cfg = settings()
    if not b or not b['tunnel_id']:
        raise CFError('固定隧道尚未创建完成')
    api('PUT', f"/accounts/{cfg['account_id']}/cfd_tunnel/{b['tunnel_id']}/configurations", {
        'config': {'ingress': [{'hostname': b['hostname'], 'service': f'http://127.0.0.1:{port}'}, {'service': 'http_status:404'}]}})
    patch_binding(node_id, route_port=port)


def provision(node_id, hostname, port):
    with LOCK:
        cfg = settings(); hostname = valid_hostname(hostname or f'node-{node_id}.' + cfg['zone_name'])
        b = binding(node_id)
        if b and b['hostname'] != hostname:
            raise CFError('已有固定隧道绑定，不能在部署中更换域名')
        if not b:
            records = api('GET', f"/zones/{cfg['zone_id']}/dns_records?" + urlencode({'name': hostname, 'per_page': 100}))
            if records:
                raise CFError('该域名已有 DNS 记录，请选择未使用的子域名')
            c = db.get_conn()
            c.execute('INSERT INTO node_cloudflare(node_id,hostname,route_port,applied_port,created_at) VALUES (?,?,?,?,?)',
                      (node_id, hostname, port, port, db.utcnow())); c.commit()
            b = binding(node_id)
        if not b['tunnel_id']:
            created = api('POST', f"/accounts/{cfg['account_id']}/cfd_tunnel", {'name': f'auto-argo-node-{node_id}-{secrets.token_hex(4)}', 'config_src': 'cloudflare'})
            patch_binding(node_id, tunnel_id=created['id'])
        b = binding(node_id)
        if not b['tunnel_token']:
            token = api('GET', f"/accounts/{cfg['account_id']}/cfd_tunnel/{b['tunnel_id']}/token")
            if not isinstance(token, str) or len(token) < 20:
                raise CFError('Cloudflare 未返回有效的隧道运行凭据')
            patch_binding(node_id, tunnel_token=encrypt(token))
        if b['status'] != 'active':
            route(node_id, port)
        b = binding(node_id)
        if not b['dns_id']:
            records = api('GET', f"/zones/{cfg['zone_id']}/dns_records?" + urlencode({'name': hostname, 'per_page': 100}))
            if records:
                raise CFError('该域名在部署期间新增了 DNS 记录，已停止操作以免覆盖')
            record = api('POST', f"/zones/{cfg['zone_id']}/dns_records", {'type': 'CNAME', 'name': hostname,
                'content': b['tunnel_id'] + '.cfargotunnel.com', 'proxied': True, 'ttl': 1,
                'comment': 'Created by auto-argo-node; node ' + str(node_id)})
            patch_binding(node_id, dns_id=record['id'])
        return binding(node_id)


def credentials(node_id, refresh=False):
    with LOCK:
        b = binding(node_id)
        if not b or not b['tunnel_token'] or not b['dns_id']:
            raise CFError('固定隧道配置尚未完成，请在面板重试任务')
        if refresh:
            cfg = settings()
            token = api('GET', f"/accounts/{cfg['account_id']}/cfd_tunnel/{b['tunnel_id']}/token")
            if not isinstance(token, str) or len(token) < 20:
                raise CFError('Cloudflare 隧道运行凭据无效')
            patch_binding(node_id, tunnel_token=encrypt(token)); b = binding(node_id)
        return {'hostname': b['hostname'], 'tunnel_token': decrypt(b['tunnel_token']), 'route_port': b['route_port']}


def commit(node_id, port):
    if binding(node_id):
        patch_binding(node_id, applied_port=port, route_port=port, status='active')


def rollback_route(node_id):
    with LOCK:
        b = binding(node_id)
        if b and b['route_port'] != b['applied_port']:
            route(node_id, b['applied_port'])


def cleanup(node_id):
    with LOCK:
        b = binding(node_id)
        if not b:
            return
        cfg = settings()
        if b['dns_id']:
            record = api('GET', f"/zones/{cfg['zone_id']}/dns_records/{b['dns_id']}", missing_ok=True)
            if record:
                if record.get('name') != b['hostname'] or record.get('type') != 'CNAME' or record.get('content') != b['tunnel_id'] + '.cfargotunnel.com':
                    raise CFError('固定域名的 DNS 已被修改，停止清理以免删除其他配置')
                api('DELETE', f"/zones/{cfg['zone_id']}/dns_records/{b['dns_id']}", missing_ok=True)
            patch_binding(node_id, dns_id='')
        if b['tunnel_id']:
            # Only this panel-created, per-node tunnel is affected.
            api('DELETE', f"/accounts/{cfg['account_id']}/cfd_tunnel/{b['tunnel_id']}/connections", missing_ok=True)
            api('DELETE', f"/accounts/{cfg['account_id']}/cfd_tunnel/{b['tunnel_id']}", missing_ok=True)
        c = db.get_conn(); c.execute('DELETE FROM node_cloudflare WHERE node_id=?', (node_id,)); c.commit()


def register(app, require_login):
    def owner(request):
        require_login(request)
        if request.headers.get('origin') and urlsplit(request.headers['origin']).netloc != request.headers.get('host'):
            raise HTTPException(403, '请求来源不匹配')

    @app.get('/api/cloudflare')
    def get_settings(request: Request):
        owner(request)
        return status()

    @app.put('/api/cloudflare')
    def put_settings(body: ConfigBody, request: Request):
        owner(request)
        try:
            return configure(body)
        except CFError as exc:
            raise HTTPException(502, str(exc))
