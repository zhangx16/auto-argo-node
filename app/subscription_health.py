"""Opt-in export quarantine based on distinct, fresh forwarding observations."""
import hashlib
import json
import time
from fastapi import HTTPException,Request
from pydantic import BaseModel,Field
from . import db


def init():
    db.get_conn().executescript('''
      CREATE TABLE IF NOT EXISTS subscription_health_policy(subscription_id INTEGER PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 0,fail_count INTEGER NOT NULL DEFAULT 3,fail_minutes INTEGER NOT NULL DEFAULT 5,
        recover_count INTEGER NOT NULL DEFAULT 2,recover_minutes INTEGER NOT NULL DEFAULT 5);
      CREATE TABLE IF NOT EXISTS subscription_health_nodes(subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,isolated INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,successes INTEGER NOT NULL DEFAULT 0,fail_since REAL NOT NULL DEFAULT 0,success_since REAL NOT NULL DEFAULT 0,
        sample_key TEXT NOT NULL DEFAULT '',last_sample REAL NOT NULL DEFAULT 0,last_state TEXT NOT NULL DEFAULT '',changed REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(subscription_id,node_id));
    ''');db.get_conn().commit()
    from . import schema
    schema.columns('subscription_health_nodes',[('identity',"TEXT NOT NULL DEFAULT ''")])


class Policy(BaseModel):
    enabled:bool=False
    fail_count:int=Field(default=3,ge=2,le=20)
    fail_minutes:int=Field(default=5,ge=1,le=1440)
    recover_count:int=Field(default=2,ge=2,le=20)
    recover_minutes:int=Field(default=5,ge=1,le=1440)


def policy(subscription_id):
    r=db.get_conn().execute('SELECT * FROM subscription_health_policy WHERE subscription_id=?',(subscription_id,)).fetchone()
    return dict(r) if r else Policy().model_dump()|{'subscription_id':subscription_id}


def isolated_ids(subscription_id):
    if not policy(subscription_id)['enabled']:return set()
    return {r[0] for r in db.get_conn().execute('SELECT node_id FROM subscription_health_nodes WHERE subscription_id=? AND isolated=1',(subscription_id,))}


def describe(subscription_id):
    p=policy(subscription_id)
    rows=[dict(r) for r in db.get_conn().execute('''SELECT h.node_id,n.remark,h.isolated,h.failures,h.successes,h.last_state,h.last_sample,h.changed
      FROM subscription_health_nodes h JOIN subscription_nodes m ON m.node_id=h.node_id AND m.subscription_id=h.subscription_id
      JOIN nodes n ON n.id=h.node_id WHERE h.subscription_id=? ORDER BY h.isolated DESC,n.id''',(subscription_id,))]
    return {'policy':p,'nodes':rows,'isolated_count':sum(bool(r['isolated']) for r in rows) if p['enabled'] else 0,
      'note':'仅影响本订阅导出，不删除节点或停止服务器。依据自建节点的实际转发检测；外部节点不自动测活或隔离。维护、未知和过期结果不计数。客户端需更新订阅后生效。'}


def notify(subscription_id,node_id,isolated):
    from . import telemetry
    sub=db.get_conn().execute('SELECT name FROM subscriptions WHERE id=?',(subscription_id,)).fetchone();node=db.get_node(node_id)
    if not sub or not node:return
    telemetry.enqueue(f'subhealth:{subscription_id}:{node_id}:{time.time_ns()}',telemetry.message_text(
      '🟠 订阅节点暂时隔离' if isolated else '🟢 订阅节点恢复导出',
      [('订阅',sub['name']),('节点',node['remark']),('结果','已从本订阅暂时排除' if isolated else '通过恢复观察，重新加入本订阅')],
      '节点记录与协议服务保留；客户端更新订阅后生效。'),category='health',route='subscription-module',priority=1)


def observe(node_id,state,at,revision,link,now=None):
    from . import operations,agent
    now=time.time() if now is None else now
    c=db.get_conn()
    if state not in ('ok','failed') or agent.age(at)>600:return
    rows=c.execute('''SELECT p.* FROM subscription_health_policy p JOIN subscription_nodes m ON m.subscription_id=p.subscription_id
      WHERE p.enabled=1 AND m.node_id=?''',(node_id,)).fetchall()
    for p in rows:
        sid=p['subscription_id']
        c.execute('INSERT OR IGNORE INTO subscription_health_nodes(subscription_id,node_id) VALUES (?,?)',(sid,node_id))
        if operations.maintenance(node_id):
            c.execute('UPDATE subscription_health_nodes SET failures=0,successes=0,fail_since=0,success_since=0 WHERE subscription_id=? AND node_id=?',(sid,node_id));continue
        key=hashlib.sha256(f'{at}:{revision}:{link}:{state}'.encode()).hexdigest()
        r=c.execute('SELECT * FROM subscription_health_nodes WHERE subscription_id=? AND node_id=?',(sid,node_id)).fetchone()
        if r['sample_key']==key:continue
        # New samples after an extended gap start a new observation streak.
        identity=hashlib.sha256(f'{revision}:{link}'.encode()).hexdigest()
        contiguous=bool(r['last_sample'] and now-r['last_sample']<=900 and r['identity']==identity)
        fails=(r['failures'] if contiguous else 0)+1 if state=='failed' else 0
        successes=(r['successes'] if contiguous else 0)+1 if state=='ok' else 0
        fail_since=(r['fail_since'] if contiguous and r['fail_since'] else now) if fails else 0
        success_since=(r['success_since'] if contiguous and r['success_since'] else now) if successes else 0
        c.execute('''UPDATE subscription_health_nodes SET failures=?,successes=?,fail_since=?,success_since=?,sample_key=?,last_sample=?,last_state=?,identity=?
            WHERE subscription_id=? AND node_id=?''',(fails,successes,fail_since,success_since,key,now,state,identity,sid,node_id))
    c.commit();tick(now)


def tick(now=None):
    from . import operations
    now=time.time() if now is None else now;c=db.get_conn()
    rows=c.execute('''SELECT h.*,p.fail_count,p.fail_minutes,p.recover_count,p.recover_minutes FROM subscription_health_nodes h
      JOIN subscription_health_policy p ON p.subscription_id=h.subscription_id JOIN subscription_nodes m ON m.subscription_id=h.subscription_id AND m.node_id=h.node_id
      WHERE p.enabled=1''').fetchall()
    for r in rows:
        if operations.maintenance(r['node_id']):
            c.execute('UPDATE subscription_health_nodes SET failures=0,successes=0,fail_since=0,success_since=0 WHERE subscription_id=? AND node_id=?',(r['subscription_id'],r['node_id']));continue
        if not r['last_sample'] or now-r['last_sample']>600:continue
        a=c.execute('SELECT a.probe_status,a.probe_at,a.applied_revision,n.share_link,n.status FROM node_agents a JOIN nodes n ON n.id=a.node_id WHERE a.node_id=?',(r['node_id'],)).fetchone()
        if not a or a['probe_status'] not in ('ok','failed') or a['status']=='uninstalling':continue
        current_key=hashlib.sha256(f"{a['probe_at']}:{a['applied_revision']}:{a['share_link']}:{a['probe_status']}".encode()).hexdigest()
        if current_key!=r['sample_key']:continue
        transition=(not r['isolated'] and r['failures']>=r['fail_count'] and now-r['fail_since']>=r['fail_minutes']*60)
        recovery=(r['isolated'] and r['successes']>=r['recover_count'] and now-r['success_since']>=r['recover_minutes']*60)
        if transition or recovery:
            isolated=int(transition)
            changed=c.execute('UPDATE subscription_health_nodes SET isolated=?,changed=? WHERE subscription_id=? AND node_id=? AND isolated=?',(isolated,now,r['subscription_id'],r['node_id'],r['isolated'])).rowcount
            if changed:
                try:notify(r['subscription_id'],r['node_id'],isolated)
                except Exception:pass
    # Drop observations that no longer belong to a subscription, not node records.
    c.execute('DELETE FROM subscription_health_nodes WHERE NOT EXISTS (SELECT 1 FROM subscription_nodes m WHERE m.subscription_id=subscription_health_nodes.subscription_id AND m.node_id=subscription_health_nodes.node_id)')
    c.commit()


def chains(subscription,configured):
    isolated=isolated_ids(subscription['id'])
    return [chain for chain in configured if not any(h['kind']=='managed' and h['id'] in isolated for h in chain['hops'])]


def register(app,owner):
    @app.get('/api/subscriptions/{subscription_id}/health')
    def get_policy(subscription_id:int,request:Request):
        owner(request)
        from . import subscriptions
        subscriptions.get(subscription_id);return describe(subscription_id)

    @app.put('/api/subscriptions/{subscription_id}/health')
    def set_policy(subscription_id:int,body:Policy,request:Request):
        owner(request)
        from . import subscriptions,agent
        with agent.LOCK:
            subscriptions.get(subscription_id)
            # A policy change begins a fresh observation window, avoiding stale exclusions.
            db.get_conn().execute('DELETE FROM subscription_health_nodes WHERE subscription_id=?',(subscription_id,))
            db.get_conn().execute('INSERT INTO subscription_health_policy VALUES (?,?,?,?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET enabled=excluded.enabled,fail_count=excluded.fail_count,fail_minutes=excluded.fail_minutes,recover_count=excluded.recover_count,recover_minutes=excluded.recover_minutes',
                (subscription_id,int(body.enabled),body.fail_count,body.fail_minutes,body.recover_count,body.recover_minutes));db.get_conn().commit()
            return describe(subscription_id)
