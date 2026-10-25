"""Maintenance and repair authorization. No additional node daemon."""
import json
import time
from typing import Literal
from fastapi import HTTPException, Request, Header
from pydantic import BaseModel, Field
from . import db, events

AGENT_VERSION = 8


def init():
    db.get_conn().executescript('''
    CREATE TABLE IF NOT EXISTS maintenance(scope TEXT NOT NULL,target TEXT NOT NULL,
      until REAL NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(scope,target));
    CREATE TABLE IF NOT EXISTS repair_policy(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      limit_count INTEGER NOT NULL DEFAULT 3,window_minutes INTEGER NOT NULL DEFAULT 60,
      tripped INTEGER NOT NULL DEFAULT 0,reset_at REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS repair_permits(id INTEGER PRIMARY KEY,node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS repair_permits_node ON repair_permits(node_id,created);
    ''')
    db.get_conn().commit()


def maintenance(node_id):
    c=db.get_conn()
    r=c.execute('''SELECT scope,until,reason FROM maintenance WHERE until>? AND
      ((scope='node' AND target=?) OR (scope='host' AND target IN
      (SELECT host_id FROM monitor_nodes WHERE node_id=?))) ORDER BY until DESC LIMIT 1''',
      (time.time(),str(node_id),node_id)).fetchone()
    return dict(r) if r else None


def host_maintenance(host_id):
    return bool(db.get_conn().execute("SELECT 1 FROM maintenance WHERE scope='host' AND target=? AND until>?",(host_id,time.time())).fetchone())


def policy(node_id):
    c=db.get_conn()
    r=c.execute('SELECT * FROM repair_policy WHERE node_id=?',(node_id,)).fetchone()
    p=dict(r) if r else dict(limit_count=3,window_minutes=60,tripped=0,reset_at=0)
    p['attempts']=c.execute('SELECT count(*) FROM repair_permits WHERE node_id=? AND created>?',
        (node_id,max(time.time()-p['window_minutes']*60,p['reset_at']))).fetchone()[0]
    p.pop('node_id',None)
    return p


def control(node_id):
    p=policy(node_id);m=maintenance(node_id)
    return {'maintenance':m,'repair':p,'repair_allowed':not m and not p['tripped'] and p['attempts']<p['limit_count']}


def permit(node_id):
    """Called under agent.LOCK. Count reservations conservatively, even if callback is lost."""
    from . import agent
    a=agent.row(node_id);c=db.get_conn();p=policy(node_id)
    if not a or not a['management_enabled'] or maintenance(node_id):return {'allowed':False,'reason':'维护中或未启用管理'}
    if db.get_node(node_id)['status']=='uninstalling' or c.execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone():
        return {'allowed':False,'reason':'有配置任务正在执行'}
    if p['tripped'] or p['attempts']>=p['limit_count']:
        if not p['tripped']:
            c.execute('INSERT INTO repair_policy(node_id,tripped) VALUES (?,1) ON CONFLICT(node_id) DO UPDATE SET tripped=1',(node_id,))
            events.add(node_id,'repair_limit','自动修复达到上限，已暂停；请检查故障详情后手动解除','warning');c.commit()
        return {'allowed':False,'reason':'修复上限已触发，需手动解除'}
    last=c.execute('SELECT max(created) FROM repair_permits WHERE node_id=?',(node_id,)).fetchone()[0]
    if last and time.time()-last<300:return {'allowed':False,'reason':'修复冷却中'}
    c.execute('INSERT INTO repair_permits(node_id,created) VALUES (?,?)',(node_id,time.time()))
    events.add(node_id,'repair_permit',f'已授权自动修复：{p["attempts"]+1}/{p["limit_count"]} 次（{p["window_minutes"]} 分钟窗口）')
    if p['attempts']+1>=p['limit_count']:
        c.execute('INSERT INTO repair_policy(node_id,tripped) VALUES (?,1) ON CONFLICT(node_id) DO UPDATE SET tripped=1',(node_id,))
        events.add(node_id,'repair_limit','本次为窗口内最后一次修复授权，后续自动修复已暂停；请检查结果后手动解除','warning')
    c.commit()
    return {'allowed':True}


def set_maintenance(scope,target,minutes,reason):
    c=db.get_conn()
    if scope=='node':
        if not target.isdigit() or not db.get_node(int(target)):raise HTTPException(404,'节点不存在')
        ids=[int(target)]
    else:
        if not c.execute('SELECT 1 FROM monitor_hosts WHERE id=?',(target,)).fetchone():raise HTTPException(404,'服务器不存在')
        ids=[r[0] for r in c.execute('SELECT node_id FROM monitor_nodes WHERE host_id=?',(target,))]
        if minutes:c.execute("DELETE FROM notification_queue WHERE event_key LIKE ?",('resource:'+target+':%',))
    c.execute('INSERT INTO maintenance VALUES (?,?,?,?) ON CONFLICT(scope,target) DO UPDATE SET until=excluded.until,reason=excluded.reason',
        (scope,target,time.time()+minutes*60 if minutes else 0,reason))
    for nid in ids:
        events.add(nid,'maintenance',f'开始维护 {minutes} 分钟：{reason}' if minutes else '已结束维护；重新等待健康验证')
        c.execute('DELETE FROM notification_states WHERE node_id=?',(nid,))
        c.execute("DELETE FROM notification_queue WHERE event_key LIKE ? OR event_key IN (SELECT 'event:'||id FROM node_events WHERE node_id=? AND kind IN ('repair','repair_limit'))",(f'node:{nid}:%',nid))
    c.commit()


def tick():
    c=db.get_conn()
    for r in c.execute('SELECT scope,target FROM maintenance WHERE until>0 AND until<=?',(time.time(),)).fetchall():
        ids=[int(r['target'])] if r['scope']=='node' else [n[0] for n in c.execute('SELECT node_id FROM monitor_nodes WHERE host_id=?',(r['target'],))]
        for nid in ids:
            if db.get_node(nid):events.add(nid,'maintenance','维护时间已结束，恢复健康验证与受控修复')
        c.execute('DELETE FROM maintenance WHERE scope=? AND target=?',(r['scope'],r['target']))
    c.execute('DELETE FROM repair_permits WHERE created<?',(time.time()-90*86400,))
    c.execute("DELETE FROM maintenance WHERE scope='node' AND target NOT IN (SELECT CAST(id AS TEXT) FROM nodes)")
    c.execute("DELETE FROM maintenance WHERE scope='host' AND target NOT IN (SELECT id FROM monitor_hosts)")
    c.commit()


def diagnostics(node_id):
    from . import agent, monitor
    n=db.get_node(node_id);a=agent.row(node_id)
    if not n or not a:raise HTTPException(404,'未找到受管节点')
    collector=monitor.collector_available() if a['health_mode']=='monitor' else True
    fresh=agent.age(a['last_report_at'])<180
    sampled=fresh and collector and a['health_mode']!='none'
    checks=[{'name':'心跳 / 探针','state':'unknown' if not collector or a['health_mode']=='none' else 'ok' if fresh else 'failed','at':a['last_report_at']},
      {'name':'协议进程','state':('ok' if a['process_alive'] else 'failed') if sampled else 'unknown','at':a['last_report_at']},
      {'name':'本地监听','state':('ok' if a['local_ws'] else 'failed') if sampled else 'unknown','at':a['last_report_at']},
      {'name':'实际转发（面板视角）','state':a['probe_status'] if agent.age(a['probe_at'])<600 else 'unknown','at':a['probe_at']}]
    if a['health_mode']=='legacy' and json.loads(a['desired_json']).get('protocol','vless-ws-argo')=='vless-ws-argo':
        checks.insert(3,{'name':'公网 WebSocket','state':('ok' if a['public_ws'] else 'failed') if sampled else 'unknown','at':a['last_report_at']})
    # Do not expose raw job/probe errors, configs, domains, tokens or shell logs.
    timeline=[dict(r) for r in db.get_conn().execute("SELECT created_at,kind,level,message FROM node_events WHERE node_id=? ORDER BY id DESC LIMIT 100",(node_id,))]
    jobs=[dict(r) for r in db.get_conn().execute('SELECT revision,operation,status,created_at,finished_at FROM agent_jobs WHERE node_id=? ORDER BY revision DESC LIMIT 10',(node_id,))]
    return {'node_id':node_id,'name':n['remark'],'generated_at':db.utcnow(),'checks':checks,'control':control(node_id),
      'agent_version':a['agent_version'],'required_agent_version':AGENT_VERSION,'events':timeline,'jobs':jobs,
      'note':'心跳不等于实际转发成功；面板检测失败也可能由面板出站网络引起。旧脚本升级至 v8 后修复限制才完整生效。'}


class MaintenanceBody(BaseModel):
    scope:Literal['node','host']='node'
    target:str=Field(min_length=1,max_length=80)
    minutes:int=Field(default=30,ge=0,le=10080)
    reason:str=Field(default='计划维护',max_length=120)


class PolicyBody(BaseModel):
    limit_count:int=Field(default=3,ge=1,le=20)
    window_minutes:int=Field(default=60,ge=10,le=1440)


def register(app,owner):
    @app.put('/api/operations/maintenance')
    def maintain(body:MaintenanceBody,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:set_maintenance(body.scope,body.target,body.minutes,body.reason)
        return {'ok':True}

    @app.get('/api/nodes/{node_id}/diagnostics')
    def details(node_id:int,request:Request):
        owner(request);return diagnostics(node_id)

    @app.put('/api/nodes/{node_id}/repair-policy')
    def configure(node_id:int,body:PolicyBody,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:
            if not agent.row(node_id):raise HTTPException(404,'节点不存在')
            db.get_conn().execute('INSERT INTO repair_policy(node_id,limit_count,window_minutes) VALUES (?,?,?) ON CONFLICT(node_id) DO UPDATE SET limit_count=excluded.limit_count,window_minutes=excluded.window_minutes',(node_id,body.limit_count,body.window_minutes))
            if policy(node_id)['attempts']>=body.limit_count:
                db.get_conn().execute('UPDATE repair_policy SET tripped=1 WHERE node_id=?',(node_id,))
            events.add(node_id,'repair_policy',f'修复策略：{body.window_minutes} 分钟最多 {body.limit_count} 次');db.get_conn().commit()
            return control(node_id)

    @app.post('/api/nodes/{node_id}/repair-reset')
    def reset(node_id:int,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:
            if not agent.row(node_id):raise HTTPException(404,'节点不存在')
            db.get_conn().execute('INSERT INTO repair_policy(node_id,reset_at) VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET tripped=0,reset_at=excluded.reset_at',(node_id,time.time()))
            events.add(node_id,'repair_policy','管理员已解除修复暂停，重新开始计数');db.get_conn().commit()
            return control(node_id)

    @app.post('/agent/repair-permit')
    def authorize_repair(x_agent_token:str|None=Header(default=None),x_ssh_execution:str|None=Header(default=None)):
        from . import agent,ssh_control
        with agent.LOCK:
            a=agent.authorize(x_agent_token)
            if ssh_control.controls(a['node_id']) and x_ssh_execution!='1':return {'allowed':False,'reason':'SSH 管理模式'}
            return permit(a['node_id'])
