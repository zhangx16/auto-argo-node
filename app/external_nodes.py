"""External nodes are export-only records, never remote deployment targets."""
import hashlib
import json
import re
import sqlite3

import yaml
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from . import db, import_fetch
from .node_formats import PROTOCOLS, parse_uri, to_uri, from_config, b64decode, display_name

MAX_NODES = 1000


def init():
    c = db.get_conn()
    c.executescript('''CREATE TABLE IF NOT EXISTS external_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, uri TEXT NOT NULL,
        fingerprint TEXT NOT NULL UNIQUE, protocol TEXT NOT NULL, server TEXT NOT NULL,
        port INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, source_label TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );''')
    from . import schema
    schema.columns('external_nodes',[('source_available', 'INTEGER NOT NULL DEFAULT 1')],version=1)
    c.commit()


def available(node):
    return bool(node['enabled']) and bool(node.get('source_available',1))


class ImportBody(BaseModel):
    content: str = Field(default='', max_length=import_fetch.MAX_BYTES)
    url: str = Field(default='', max_length=4096)

    @model_validator(mode='after')
    def one_source(self):
        if bool(self.content.strip()) == bool(self.url.strip()):
            raise ValueError('请填写节点内容或订阅 URL，两者选一')
        if len(self.content.encode()) > import_fetch.MAX_BYTES:
            raise ValueError('导入内容不能超过 1 MiB')
        return self


class NodeInput(BaseModel):
    uri: str = Field(min_length=1, max_length=16384)
    name: str = Field(default='', max_length=80)


class ConfirmBody(BaseModel):
    nodes: list[NodeInput] = Field(min_length=1, max_length=MAX_NODES)
    source_label: str = Field(default='', max_length=80)


class EditBody(NodeInput):
    enabled: bool = True

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('请输入有效节点名称')
        return value.strip()


class DeleteBody(BaseModel):
    node_ids: list[int] = Field(min_length=1,max_length=MAX_NODES)

    @field_validator('node_ids')
    @classmethod
    def valid_ids(cls,value):
        if any(v<1 for v in value) or len(set(value))!=len(value):raise ValueError('请选择有效且不重复的外部节点')
        return value


def remove_many(node_ids):
    from . import agent,sources
    with agent.LOCK:
        c=db.get_conn()
        for node_id in node_ids:get(node_id)
        with c:
            for node_id in node_ids:
                sources.removed(node_id)
                c.execute('DELETE FROM external_nodes WHERE id=?',(node_id,))
    return {'ok':True,'deleted':len(node_ids)}


def get(node_id):
    r = db.get_conn().execute('SELECT * FROM external_nodes WHERE id=?', (node_id,)).fetchone()
    if not r:
        raise HTTPException(404, '外部节点不存在')
    return dict(r)


def fingerprint(p):
    # Match node identity regardless of display name or source-label changes.
    uri = to_uri(dict(p, name='node'))
    return hashlib.sha256(uri.encode()).hexdigest()


def describe(row):
    from . import regions, labels
    return labels.enrich(regions.enrich({k: bool(v) if k == 'enabled' else v for k, v in dict(row).items() if k != 'fingerprint'},'external'),'external')


class LimitedLoader(yaml.SafeLoader):
    """Bound YAML nesting and alias-heavy documents before constructing objects."""
    def __init__(self, stream):
        super().__init__(stream)
        self.nodes = self.depth = 0

    def compose_node(self, parent, index):
        self.nodes += 1; self.depth += 1
        try:
            if self.nodes > 50000 or self.depth > 40:
                raise ValueError('配置结构过大或嵌套过深')
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


def parse_content(content):
    content = content.strip().lstrip('\ufeff')
    if not content or len(content.encode()) > import_fetch.MAX_BYTES:
        raise ValueError('导入内容为空或超过 1 MiB')
    if '://' not in content and not content.startswith(('{', '[', 'proxies:', 'outbounds:')):
        try:
            decoded = b64decode(content)
            if decoded.strip():
                content = decoded.strip()
        except ValueError:
            pass
    # URI lists can contain comments or empty lines; each bad item is reported without echoing secrets.
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith('#')]
    if lines and any(re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', line) for line in lines):
        entries, flavor = lines, 'uri'
    else:
        try:
            document = yaml.load(content, Loader=LimitedLoader)
        except (yaml.YAMLError, ValueError, RecursionError):
            raise ValueError('无法解析配置，请提供节点链接、Base64、Clash YAML 或 sing-box JSON')
        if isinstance(document, dict) and isinstance(document.get('proxies'), list):
            entries, flavor = document['proxies'], 'clash'
        elif isinstance(document, dict) and isinstance(document.get('outbounds'), list):
            entries = [n for n in document['outbounds'] if not isinstance(n, dict) or n.get('type') not in ('direct', 'block', 'selector', 'urltest', 'dns')]
            flavor = 'singbox'
        elif isinstance(document, list):
            entries, flavor = document, 'uri' if all(isinstance(x, str) for x in document) else 'clash'
        else:
            raise ValueError('未找到节点列表，请检查导入格式')
    if len(entries) > MAX_NODES:
        raise ValueError('一次最多导入 1000 个节点')
    valid, errors, seen, duplicates = [], [], set(), 0
    for index, entry in enumerate(entries, 1):
        try:
            p = parse_uri(entry) if flavor == 'uri' else from_config(entry, flavor)
            identity = fingerprint(p)
            if identity in seen:
                duplicates += 1; continue
            seen.add(identity)
            valid.append(dict(name=p['name'], protocol=p['protocol'], server=p['server'], port=p['port'], uri=to_uri(p)))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            # Do not return parser exception strings: third-party inputs may contain passwords.
            if len(errors) < 100:
                errors.append({'index': index, 'message': '不支持或参数无效，请检查协议、传输、TLS、插件及高级选项'})
    return {'nodes': valid, 'errors': errors, 'total': len(entries), 'duplicates': duplicates,
            'invalid_count': len(entries) - len(valid) - duplicates}


def add(body):
    parsed = []
    for item in body.nodes:
        try:
            p = parse_uri(item.uri, item.name or None)
            parsed.append((p, fingerprint(p)))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(422, '节点参数无效，请重新解析预览')
    c = db.get_conn(); added, duplicates, ids = [], 0, []
    with c:
        for p, identity in parsed:
            existing = c.execute('SELECT id FROM external_nodes WHERE fingerprint=?', (identity,)).fetchone()
            if existing:
                duplicates += 1; ids.append(existing['id']); continue
            now = db.utcnow()
            cur = c.execute('''INSERT OR IGNORE INTO external_nodes(name,uri,fingerprint,protocol,server,port,source_label,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)''', (p['name'], to_uri(p), identity, p['protocol'], p['server'], p['port'],
                display_name(body.source_label, '') if body.source_label else '', now, now))
            if cur.rowcount:
                ids.append(cur.lastrowid); added.append(cur.lastrowid)
            else:
                ids.append(c.execute('SELECT id FROM external_nodes WHERE fingerprint=?', (identity,)).fetchone()['id'])
                duplicates += 1
    return {'added': len(added), 'duplicates': duplicates, 'node_ids': ids}


def register(app, owner):
    @app.get('/api/external-nodes')
    def list_external(request: Request):
        owner(request)
        return [describe(r) for r in db.get_conn().execute('SELECT * FROM external_nodes ORDER BY id DESC')]

    @app.post('/api/external-nodes/preview')
    def preview(body: ImportBody, request: Request):
        owner(request)
        try:
            return parse_content(import_fetch.fetch(body.url.strip()) if body.url.strip() else body.content)
        except (ValueError, TypeError, RecursionError) as exc:
            # fetch()/parse_content() only use fixed errors at this boundary.
            raise HTTPException(422, str(exc))

    @app.post('/api/external-nodes/import')
    def confirm(body: ConfirmBody, request: Request):
        owner(request)
        return add(body)

    @app.post('/api/external-nodes/bulk-delete')
    def bulk_delete(body:DeleteBody,request:Request):
        owner(request)
        return remove_many(body.node_ids)

    @app.put('/api/external-nodes/{node_id}')
    def edit(node_id: int, body: EditBody, request: Request):
        owner(request)
        from . import agent
        with agent.LOCK:
            get(node_id)
            try:
                p = parse_uri(body.uri, body.name)
                identity = fingerprint(p)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise HTTPException(422, '节点参数无效，请检查链接')
            c = db.get_conn()
            from . import sources
            owning=c.execute('SELECT source_id FROM external_source_nodes WHERE node_id=? AND owned=1',(node_id,)).fetchone()
            if owning:identity=sources.storage_identity(owning['source_id'],identity)
            other = c.execute('SELECT id FROM external_nodes WHERE fingerprint=? AND id<>?', (identity, node_id)).fetchone()
            if other:
                raise HTTPException(409, '已有相同配置的外部节点')
            from . import sources
            try:
                with c:
                    sources.edited(node_id,body)
                    c.execute('''UPDATE external_nodes SET name=?,uri=?,fingerprint=?,protocol=?,server=?,port=?,enabled=?,updated_at=? WHERE id=?''',
                              (p['name'], to_uri(p), identity, p['protocol'], p['server'], p['port'], int(body.enabled), db.utcnow(), node_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, '已有相同配置的外部节点')
            return describe(get(node_id))

    @app.delete('/api/external-nodes/{node_id}')
    def delete(node_id: int, request: Request):
        owner(request)
        return remove_many([node_id])
