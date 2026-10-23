"""Encrypted, scheduled public subscription imports with atomic, identity-preserving updates."""
from collections import Counter
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, field_validator
from . import db, cloudflare, external_nodes, import_fetch

LOCK=threading.Lock()
RUNNING=None
STOP=threading.Event()
WORKER=None

class SourceBody(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    url:SecretStr=Field(default=SecretStr(''),max_length=4096)
    interval_minutes:int=Field(default=360,ge=15,le=10080)
    enabled:bool=True
    subscription_ids:list[int]=Field(default_factory=list,max_length=100)
    @field_validator('name')
    @classmethod
    def valid_name(cls,value):
        value=value.strip()
        if not value or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('来源名称无效')
        return value
    @field_validator('subscription_ids')
    @classmethod
    def valid_ids(cls,value):
        if any(v<1 for v in value):raise ValueError('订阅编号无效')
        return list(dict.fromkeys(value))

def init():
    c=db.get_conn()
    c.executescript('''CREATE TABLE IF NOT EXISTS external_sources(
      id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,url_cipher TEXT NOT NULL,
      interval_minutes INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,revision INTEGER NOT NULL DEFAULT 1,
      next_run INTEGER NOT NULL DEFAULT 0,last_checked TEXT NOT NULL DEFAULT '',last_success TEXT NOT NULL DEFAULT '',
      last_error TEXT NOT NULL DEFAULT '',stats_json TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS external_source_nodes(source_id INTEGER NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
      remote_key TEXT NOT NULL,node_id INTEGER REFERENCES external_nodes(id) ON DELETE SET NULL,
      remote_name TEXT NOT NULL,fingerprint TEXT NOT NULL,owned INTEGER NOT NULL DEFAULT 1,
      present INTEGER NOT NULL DEFAULT 1,excluded INTEGER NOT NULL DEFAULT 0,name_override INTEGER NOT NULL DEFAULT 0,
      config_override INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(source_id,remote_key));
    CREATE INDEX IF NOT EXISTS idx_source_node ON external_source_nodes(node_id);
    CREATE TABLE IF NOT EXISTS external_source_subscriptions(source_id INTEGER NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
      subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,PRIMARY KEY(source_id,subscription_id));''')
    c.execute("UPDATE external_sources SET next_run=0 WHERE enabled=1 AND last_checked>last_success")
    c.commit();STOP.clear()

def get(source_id):
    r=db.get_conn().execute('SELECT * FROM external_sources WHERE id=?',(source_id,)).fetchone()
    if not r:raise HTTPException(404,'订阅来源不存在')
    return dict(r)

def describe(row):
    result={k:v for k,v in row.items() if k not in ('url_cipher','stats_json')}
    result['enabled']=bool(row['enabled']);result['stats']=json.loads(row['stats_json'])
    result['running']=RUNNING==row['id']
    result['subscription_ids']=[r[0] for r in db.get_conn().execute('SELECT subscription_id FROM external_source_subscriptions WHERE source_id=?',(row['id'],))]
    result['node_count']=db.get_conn().execute('SELECT count(*) FROM external_source_nodes WHERE source_id=? AND present=1 AND excluded=0',(row['id'],)).fetchone()[0]
    return result

def save(body,source_id=None):
    old=get(source_id) if source_id else None
    raw=body.url.get_secret_value().strip()
    if raw:
        try:import_fetch.public_target(raw)
        except ValueError as e:raise HTTPException(422,str(e))
        cipher=cloudflare.encrypt(raw)
    elif old:cipher=old['url_cipher']
    else:raise HTTPException(422,'请填写订阅来源 URL')
    c=db.get_conn()
    for sid in body.subscription_ids:
        if not c.execute('SELECT 1 FROM subscriptions WHERE id=?',(sid,)).fetchone():raise HTTPException(422,'选择的目标订阅不存在')
    if not old and c.execute('SELECT count(*) FROM external_sources').fetchone()[0]>=50:raise HTTPException(409,'最多保存 50 个订阅来源')
    with c:
        if old:
            c.execute('UPDATE external_sources SET name=?,url_cipher=?,interval_minutes=?,enabled=?,revision=revision+1,next_run=0,last_error=\'\' WHERE id=?',
                (body.name,cipher,body.interval_minutes,int(body.enabled),source_id))
        else:
            source_id=c.execute('INSERT INTO external_sources(name,url_cipher,interval_minutes,enabled) VALUES (?,?,?,?)',
                (body.name,cipher,body.interval_minutes,int(body.enabled))).lastrowid
        c.execute('DELETE FROM external_source_subscriptions WHERE source_id=?',(source_id,))
        c.executemany('INSERT INTO external_source_subscriptions VALUES (?,?)',[(source_id,sid) for sid in body.subscription_ids])
    return describe(get(source_id))

def key_for(item,counts):
    identity=external_nodes.fingerprint(external_nodes.parse_uri(item['uri'],item['name']))
    name_key=item['protocol']+'\0'+item['name']
    key='name:'+hashlib.sha256(name_key.encode()).hexdigest() if counts[name_key]==1 else 'fp:'+identity
    return key,identity

def storage_identity(source_id,identity):
    return hashlib.sha256(f'external-source:{source_id}:{identity}'.encode()).hexdigest()

def apply(source_id,revision,parsed):
    """Caller holds the shared panel mutation lock. Parse failures never alter prior nodes."""
    if not parsed['nodes'] or parsed['invalid_count']:
        raise ValueError('来源为空或含无效节点，已保留原有节点；请检查来源内容')
    c=db.get_conn();source=get(source_id)
    if source['revision']!=revision or not source['enabled']:return False
    counts=Counter(item['protocol']+'\0'+item['name'] for item in parsed['nodes'])
    previous=[dict(r) for r in c.execute('SELECT * FROM external_source_nodes WHERE source_id=?',(source_id,))]
    seen=set();stats={'added':0,'updated':0,'missing':0,'ignored':0,'shared':0}
    with c:
        for item in parsed['nodes']:
            remote_key,identity=key_for(item,counts)
            stored_identity=storage_identity(source_id,identity)
            match=next((r for r in previous if r['fingerprint']==identity and r['remote_key'] not in seen),None)
            if match is None:match=next((r for r in previous if r['remote_key']==remote_key),None)
            if match:remote_key=match['remote_key']
            seen.add(remote_key)
            if match and match['excluded']:
                stats['ignored']+=1;continue
            node=c.execute('SELECT * FROM external_nodes WHERE id=?',(match['node_id'],)).fetchone() if match and match['node_id'] else None
            owned=bool(match['owned']) if node else True
            if node and owned:
                p=external_nodes.parse_uri(item['uri'],node['name'] if match['name_override'] else item['name'])
                if not match['config_override']:
                    conflict=c.execute('SELECT id FROM external_nodes WHERE fingerprint=? AND id<>?',(stored_identity,node['id'])).fetchone()
                    if conflict:raise ValueError('来源节点与已有配置冲突，已保留原有节点')
                    uri=external_nodes.to_uri(p)
                    changed=(node['uri']!=uri or not node['source_available'])
                    c.execute('UPDATE external_nodes SET name=?,uri=?,fingerprint=?,protocol=?,server=?,port=?,source_available=1,updated_at=? WHERE id=?',
                        (p['name'],uri,stored_identity,p['protocol'],p['server'],p['port'],db.utcnow(),node['id']))
                    if node['server']!=p['server']:
                        c.execute('UPDATE external_regions SET checked=0 WHERE node_id=?',(node['id'],))
                    stats['updated']+=int(changed)
                else:
                    c.execute('UPDATE external_nodes SET source_available=1 WHERE id=?',(node['id'],));stats['ignored']+=1
                node_id=node['id']
            elif node and not owned and node['fingerprint']==identity:
                node_id=node['id'];stats['shared']+=1
            else:
                duplicate=c.execute('SELECT id FROM external_nodes WHERE fingerprint=?',(stored_identity,)).fetchone()
                if duplicate:
                    node_id=duplicate['id'];owned=False;stats['shared']+=1
                else:
                    p=external_nodes.parse_uri(item['uri'],item['name']);now=db.utcnow()
                    node_id=c.execute('INSERT INTO external_nodes(name,uri,fingerprint,protocol,server,port,source_label,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                        (p['name'],external_nodes.to_uri(p),stored_identity,p['protocol'],p['server'],p['port'],source['name'],now,now)).lastrowid
                    owned=True;stats['added']+=1
            if match:
                c.execute('UPDATE external_source_nodes SET node_id=?,remote_name=?,fingerprint=?,owned=?,present=1 WHERE source_id=? AND remote_key=?',
                    (node_id,item['name'],identity,int(owned),source_id,remote_key))
            else:
                c.execute('INSERT INTO external_source_nodes(source_id,remote_key,node_id,remote_name,fingerprint,owned) VALUES (?,?,?,?,?,?)',
                    (source_id,remote_key,node_id,item['name'],identity,int(owned)))
            for target in c.execute('SELECT subscription_id FROM external_source_subscriptions WHERE source_id=?',(source_id,)).fetchall():
                sid=target['subscription_id']
                position=c.execute('SELECT coalesce(max(position),-1)+1 FROM subscription_external_nodes WHERE subscription_id=?',(sid,)).fetchone()[0]
                c.execute('INSERT OR IGNORE INTO subscription_external_nodes VALUES (?,?,?)',(sid,node_id,position))
        for old in previous:
            if old['remote_key'] in seen:continue
            c.execute('UPDATE external_source_nodes SET present=0 WHERE source_id=? AND remote_key=?',(source_id,old['remote_key']))
            if old['node_id'] and old['owned'] and not old['excluded']:
                c.execute('UPDATE external_nodes SET source_available=0 WHERE id=?',(old['node_id'],))
                stats['missing']+=int(bool(old['present']))
        c.execute('UPDATE external_sources SET last_success=?,last_error=\'\',stats_json=? WHERE id=?',(db.utcnow(),json.dumps(stats),source_id))
    return True

def edited(node_id,body):
    c=db.get_conn()
    for r in c.execute('SELECT * FROM external_source_nodes WHERE node_id=? AND owned=1',(node_id,)).fetchall():
        p=external_nodes.parse_uri(body.uri,body.name)
        changed=external_nodes.fingerprint(p)!=r['fingerprint']
        c.execute('UPDATE external_source_nodes SET name_override=?,config_override=? WHERE source_id=? AND remote_key=?',
            (int(body.name!=r['remote_name']),int(changed),r['source_id'],r['remote_key']))

def removed(node_id):
    db.get_conn().execute('UPDATE external_source_nodes SET excluded=1 WHERE node_id=?',(node_id,))

def schedule(source_id):
    global RUNNING,WORKER
    with LOCK:
        if RUNNING is not None:return False
        source=get(source_id)
        if not source['enabled']:return False
        RUNNING=source_id
    c=db.get_conn();c.execute('UPDATE external_sources SET last_checked=?,next_run=? WHERE id=?',(db.utcnow(),int(time.time())+source['interval_minutes']*60,source_id));c.commit()
    def run():
        global RUNNING
        try:
            content=import_fetch.fetch(cloudflare.decrypt(source['url_cipher']))
            parsed=external_nodes.parse_content(content)
            from . import agent
            with agent.LOCK:
                if not STOP.is_set():apply(source_id,source['revision'],parsed)
        except Exception as exc:
            message=('同步失败：'+str(exc)[:180]+'；原有节点已保留') if isinstance(exc,ValueError) else '同步失败：请检查 URL、来源格式、节点冲突及公网连接；原有节点已保留'
            c=db.get_conn()
            c.execute('UPDATE external_sources SET last_error=? WHERE id=? AND revision=?',(message,source_id,source['revision']));c.commit()
        finally:
            with LOCK:RUNNING=None
            if getattr(db._local,'conn',None):db._local.conn.close();db._local.conn=None
    WORKER=threading.Thread(target=run,daemon=True,name='subscription-refresh')
    try:WORKER.start()
    except RuntimeError:
        with LOCK:RUNNING=None
        return False
    return True

def tick():
    if STOP.is_set() or RUNNING is not None:return
    row=db.get_conn().execute('SELECT id FROM external_sources WHERE enabled=1 AND next_run<=? ORDER BY next_run,id LIMIT 1',(int(time.time()),)).fetchone()
    if row:schedule(row['id'])

def register(app,owner):
    @app.get('/api/external-sources')
    def listing(request:Request):
        owner(request);return [describe(dict(r)) for r in db.get_conn().execute('SELECT * FROM external_sources ORDER BY id DESC')]
    @app.post('/api/external-sources')
    def create(body:SourceBody,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:return save(body)
    @app.put('/api/external-sources/{source_id}')
    def edit(source_id:int,body:SourceBody,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:return save(body,source_id)
    @app.post('/api/external-sources/{source_id}/refresh')
    def refresh(source_id:int,request:Request):
        owner(request)
        if not schedule(source_id):raise HTTPException(409,'来源未启用或已有同步任务运行，请稍后重试')
        return {'scheduled':True}
    @app.post('/api/external-sources/{source_id}/restore-excluded')
    def restore(source_id:int,request:Request):
        owner(request);get(source_id)
        from . import agent
        with agent.LOCK:
            c=db.get_conn();c.execute('UPDATE external_source_nodes SET excluded=0 WHERE source_id=?',(source_id,));c.execute('UPDATE external_sources SET next_run=0 WHERE id=?',(source_id,));c.commit()
        return {'ok':True}
    @app.delete('/api/external-sources/{source_id}')
    def delete(source_id:int,request:Request):
        owner(request);get(source_id)
        from . import agent
        with agent.LOCK:
            c=db.get_conn()
            with c:
                c.execute('UPDATE external_nodes SET enabled=enabled*source_available,source_available=1 WHERE id IN (SELECT node_id FROM external_source_nodes WHERE source_id=? AND owned=1)',(source_id,))
                c.execute('DELETE FROM external_sources WHERE id=?',(source_id,))
        return {'ok':True,'nodes_preserved':True}
