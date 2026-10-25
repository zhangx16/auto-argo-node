"""Durable server recovery: preflight, serial protocol restore, probes and validation."""
import hashlib
import json
import time
from uuid import uuid4
from fastapi import HTTPException,Request
from pydantic import BaseModel,Field
from . import db


def init():
    db.get_conn().executescript('''
      CREATE TABLE IF NOT EXISTS recovery_runs(id TEXT PRIMARY KEY,server_id TEXT NOT NULL REFERENCES ssh_servers(id) ON DELETE CASCADE,
        status TEXT NOT NULL,created REAL NOT NULL,finished REAL NOT NULL DEFAULT 0,deadline REAL NOT NULL,position INTEGER NOT NULL DEFAULT 0,
        steps TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',connection TEXT NOT NULL,previous_pause INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS recovery_nodes(run_id TEXT NOT NULL REFERENCES recovery_runs(id) ON DELETE CASCADE,
        node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,config TEXT NOT NULL,expected_revision INTEGER NOT NULL,
        enrollment_hash TEXT NOT NULL DEFAULT '',before_link TEXT NOT NULL,maintenance_owned INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(run_id,node_id));
      CREATE INDEX IF NOT EXISTS idx_recovery_status ON recovery_runs(status,created);
    ''');db.get_conn().commit()
    from . import schema
    schema.columns('recovery_runs',[('maintenance_targets',"TEXT NOT NULL DEFAULT '[]'")])


def exists():return bool(db.get_conn().execute("SELECT 1 FROM sqlite_master WHERE name='recovery_runs'").fetchone())


def busy(server_id,include_paused=True):
    if not exists():return False
    return bool(db.get_conn().execute("SELECT 1 FROM recovery_runs WHERE server_id=? AND (status='running' OR (status='paused' AND ?))",(server_id,int(include_paused))).fetchone())


def guard_node(node_id,allowed_run=None):
    if not exists():return
    r=db.get_conn().execute("SELECT r.id FROM recovery_runs r JOIN ssh_nodes n ON n.server_id=r.server_id WHERE n.node_id=? AND r.status IN ('running','paused')",(node_id,)).fetchone()
    if r and r['id']!=allowed_run:raise HTTPException(409,'节点正在服务器恢复流程中，请先完成或取消恢复')


def enrollment_run(token_hash):
    if not exists():return None
    r=db.get_conn().execute("SELECT r.id FROM recovery_runs r JOIN recovery_nodes n ON n.run_id=r.id WHERE n.enrollment_hash=? AND r.status='running'",(token_hash,)).fetchone()
    return r['id'] if r else None


def connection(s):
    return hashlib.sha256(json.dumps({k:s.get(k) for k in ('host','port','host_key','relay_node_id','control_url','fallback_urls')},sort_keys=True).encode()).hexdigest()


def get(run_id):
    r=db.get_conn().execute('SELECT * FROM recovery_runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise HTTPException(404,'恢复流程不存在')
    return dict(r)


def public(r):
    from . import ssh_control
    nodes=[]
    for n in db.get_conn().execute('SELECT n.node_id,n.before_link,x.remark,x.share_link FROM recovery_nodes n JOIN nodes x ON x.id=n.node_id WHERE n.run_id=?',(r['id'],)):
        nodes.append({'id':n['node_id'],'name':n['remark'],'link_changed':bool(n['share_link']) and hashlib.sha256(n['share_link'].encode()).hexdigest()!=n['before_link']})
    return {k:r[k] for k in ('id','server_id','status','created','finished','position','error','deadline')}|{'steps':json.loads(r['steps']),'nodes':nodes,'server_name':ssh_control.server(r['server_id'])['name']}


class Start(BaseModel):
    node_ids:list[int]=Field(min_length=1,max_length=50)
    install_probe:bool=False
    verify_probe:bool=False
    confirmed:bool=False
    replace_queued:bool=False


class ConnectionFix(BaseModel):
    client_port:int=Field(ge=1,le=65535)
    client_address:str|None=Field(default=None,max_length=253,pattern=r'^[A-Za-z0-9:.\-]*$')


def create(server_id,body):
    from . import ssh_control,agent,operations,changes
    s=ssh_control.server(server_id)
    if body.install_probe or body.verify_probe:raise HTTPException(410,'独立资源探针已移除，请使用节点脚本恢复心跳')
    if not body.confirmed:raise HTTPException(400,'请确认已核对目标服务器与 SSH 指纹，恢复会在该服务器安装所选节点')
    if not s['credentials']:raise HTTPException(409,'请先填写并验证服务器 SSH 凭据')
    if busy(server_id) or ssh_control.busy(server_id):raise HTTPException(409,'服务器有未完成或待处理的任务')
    ids=body.node_ids
    if len(set(ids))!=len(ids):raise HTTPException(422,'节点不能重复')
    bound=set(ssh_control.node_ids(s))
    if not set(ids)<=bound:raise HTTPException(409,'所选节点未全部关联此服务器')
    agents={}
    for nid in ids:
        changes.guard(nid);a=agent.row(nid);n=db.get_node(nid)
        if not a or not n or n['status']=='uninstalling':raise HTTPException(409,'节点不存在或正在卸载')
        agents[nid]=a
    replace=[]
    for job in db.get_conn().execute("SELECT j.* FROM agent_jobs j JOIN ssh_nodes n ON n.node_id=j.node_id WHERE n.server_id=? AND j.status IN ('queued','running')",(server_id,)):
        a=agents.get(job['node_id'])
        if body.replace_queued and a and job['status']=='queued':replace.append((job['node_id'],job['revision']));continue
        if not (a and not a['bound'] and job['revision']==1 and job['operation']=='install' and job['status']=='queued'):
            raise HTTPException(409,'同机存在未完成配置任务，请先处理原任务')
    run_id=uuid4().hex;now=time.time()
    steps=[{'key':'preflight','name':'连接与系统能力检查','status':'pending'}]
    for nid in ids:
        steps.extend([{'key':'protocol','node_id':nid,'name':db.get_node(nid)['remark']+' · 安装协议','status':'pending'},
                      {'key':'forwarding','node_id':nid,'name':'验证实际转发','status':'pending'}])
    steps.append({'key':'heartbeat','name':'验证节点脚本心跳','status':'pending'})
    c=db.get_conn();c.execute('INSERT INTO recovery_runs(id,server_id,status,created,deadline,steps,connection,previous_pause) VALUES (?,?,?,?,?,?,?,?)',
        (run_id,server_id,'running',now,now+7200,json.dumps(steps),connection(s),s['automation_paused']))
    for nid,revision in replace:
        c.execute("UPDATE agent_jobs SET status='failed',finished_at=?,error='服务器恢复流程取消了原排队任务' WHERE node_id=? AND revision=? AND status='queued'",(db.utcnow(),nid,revision))
    for nid,a in agents.items():
        own=not bool(operations.maintenance(nid));n=db.get_node(nid)
        c.execute('INSERT INTO recovery_nodes VALUES (?,?,?,?,?,?,?)',(run_id,nid,a['desired_json'],a['desired_revision']+1 if a['bound'] else a['desired_revision'],'',hashlib.sha256(n['share_link'].encode()).hexdigest(),int(own)))
    c.execute('UPDATE ssh_servers SET automation_paused=1 WHERE id=?',(server_id,));c.commit()
    targets=[]
    for nid in sorted(bound):
        if not operations.maintenance(nid):
            operations.set_maintenance('node',str(nid),120,'服务器恢复 '+run_id);targets.append(['node',str(nid)])
    c.execute('UPDATE recovery_runs SET maintenance_targets=? WHERE id=?',(json.dumps(targets),run_id));c.commit()
    return public(get(run_id))


def save_steps(r,steps,position=None):
    db.get_conn().execute('UPDATE recovery_runs SET steps=?,position=? WHERE id=?',(json.dumps(steps),r['position'] if position is None else position,r['id']));db.get_conn().commit()


def pause(r,message):
    db.get_conn().execute("UPDATE recovery_runs SET status='paused',error=? WHERE id=?",(message,r['id']));db.get_conn().commit()


def complete(r,status='done'):
    from . import operations,agent
    c=db.get_conn()
    c.execute('UPDATE recovery_runs SET status=?,finished=? WHERE id=?',(status,time.time(),r['id']))
    if status=='done':c.execute("UPDATE ssh_servers SET automation_paused=?,last_error='' WHERE id=?",(r['previous_pause'],r['server_id']))
    c.commit()
    for scope,target in json.loads(r.get('maintenance_targets','[]')):
        m=c.execute('SELECT reason FROM maintenance WHERE scope=? AND target=?',(scope,target)).fetchone()
        if m and m['reason']=='服务器恢复 '+r['id'] and db.get_node(int(target)):
            operations.set_maintenance(scope,target,0,'服务器恢复完成' if status=='done' else '恢复已取消')
    try:
        from . import telemetry,ssh_control
        s=ssh_control.server(r['server_id'])
        telemetry.enqueue('recovery:'+r['id']+':'+status,telemetry.message_text('✅ 服务器恢复完成' if status=='done' else '恢复流程已取消',
            [('服务器',s['name']),('结果','所选协议与检测步骤已通过' if status=='done' else '后续步骤已停止，已执行变更保留')],
            '请查看恢复详情中的链接变更提示。'),category='tasks',route='server-module',priority=1)
    except Exception:pass


def _advance(r):
    from . import ssh_control,agent,monitor
    c=db.get_conn();now=time.time()
    s=ssh_control.server(r['server_id']);steps=json.loads(r['steps'])
    if connection(s)!=r['connection']:pause(r,'连接地址或主机指纹已变化，请核对后继续');return
    if now>r['deadline']:pause(r,'恢复流程超过两小时，请检查当前步骤后继续');return
    stored=c.execute('SELECT * FROM recovery_nodes WHERE run_id=?',(r['id'],)).fetchall()
    if not stored or any(not agent.row(n['node_id']) or json.loads(agent.row(n['node_id'])['desired_json'])!=json.loads(n['config']) for n in stored):
        pause(r,'节点或保存配置已变化，请取消当前流程后重新开始');return
    if r['position']>=len(steps):complete(r);return
    step=steps[r['position']];key=step['key'];task=None
    if step.get('task_id'):
        task=c.execute('SELECT * FROM ssh_tasks WHERE id=?',(step['task_id'],)).fetchone()
        if not task:pause(r,'步骤任务记录缺失，未重放，请人工核对');return
        if task['status'] in ('failed','unknown'):
            step['status']='failed';save_steps(r,steps);pause(r,task['error'] or '步骤执行未完成，请检查任务日志');return
        if task['status']!='done':return
        if key=='protocol':
            item=next(n for n in stored if n['node_id']==step['node_id'])
            job=c.execute('SELECT status FROM agent_jobs WHERE node_id=? AND revision=?',(step['node_id'],item['expected_revision'])).fetchone()
            if not job or job['status']!='done':
                if job and job['status'] in ('queued','running'):return
                step['status']='failed';save_steps(r,steps);pause(r,'协议安装未确认成功，请检查节点任务，未继续安装探针');return
    elif key in ('preflight','protocol','probe'):
        if ssh_control.busy(s['id']):return
        payload={};nid=step.get('node_id')
        if key=='preflight':action='preflight'
        elif key=='protocol':
            action='recover_node';a=agent.row(nid)
            pending=c.execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status='queued' AND operation='install'",(nid,)).fetchone()
            reinstall=bool(a['bound']) or not pending
            enrollment=agent.enrollment(nid,reinstall=reinstall)
            c.execute('UPDATE recovery_nodes SET enrollment_hash=?,expected_revision=? WHERE run_id=? AND node_id=?',
                (agent.row(nid)['enrollment_hash'],a['desired_revision']+1 if reinstall else a['desired_revision'],r['id'],nid));c.commit()
            payload={'script':'#!/bin/sh\nset -eu\necho ARGO_STAGE=protocol_install\n'+enrollment['command']+'\n'}
        else:
            action='recover_probe'
            nid=stored[0]['node_id']
            enrollment=monitor.enrollment_for_node(nid,s['monitor_host_id'])
            c.execute('UPDATE ssh_servers SET monitor_host_id=? WHERE id=?',(enrollment['host_id'],s['id']));c.commit()
            payload={'script':'#!/bin/sh\nset -eu\necho ARGO_STAGE=probe_install\n'+enrollment['command']+'\n'}
        queued=ssh_control.queue(s['id'],action,nid,payload)
        step.update(status='running',task_id=queued['id'],started=now);save_steps(r,steps);return
    elif key=='forwarding':
        a=agent.row(step['node_id'])
        if 'started' not in step:
            step.update(status='running',started=now)
            if agent.schedule_probe(step['node_id']):step['probe_requested']=now
            save_steps(r,steps);return
        if a['probe_status']=='ok' and agent.age(a['probe_at'])<=max(0,now-step['started'])+2:pass
        elif step.get('probe_requested') and now-step['probe_requested']>180:pause(r,'协议已安装，但实际转发未通过；请检查公网端口、隧道和连接地址');return
        elif not step.get('probe_requested') and now-step['started']>300:pause(r,'等待检测队列超时，尚未取得新的转发验证结果；请检查面板检测任务后继续');return
        else:
            if a['probe_status']!='pending' and now-step.get('checked',0)>30:
                if agent.schedule_probe(step['node_id']):step.setdefault('probe_requested',now)
                step['checked']=now;save_steps(r,steps)
            return
    elif key=='heartbeat':
        rows=[agent.row(n['node_id']) for n in stored]
        if not all(a['health_mode']=='legacy' and agent.age(a['last_report_at'])<180 for a in rows):
            step.setdefault('started',now);step['status']='running';save_steps(r,steps)
            if now-step['started']>180:pause(r,'等待节点脚本心跳，尚未确认恢复完成')
            return
    elif key=='sampling':
        h=monitor.get_host(ssh_control.server(s['id'])['monitor_host_id']);health=json.loads(h['health'])
        live={x['node_id'] for x in health.get('services',[]) if x.get('process_alive') and x.get('local_listen')}
        needed={n['node_id'] for n in stored}
        if now-h['last_seen']<90 and now-health.get('received_at',0)<90 and needed<=live:
            c.executemany("UPDATE node_agents SET health_mode='monitor' WHERE node_id=?",[(nid,) for nid in needed]);c.commit()
        else:
            step.setdefault('started',now);step['status']='running';save_steps(r,steps)
            if now-step['started']>180:pause(r,'等待探针心跳或所选节点服务采样，未确认恢复完成')
            return
    if key=='preflight':
        from . import capabilities
        try:capabilities.require_install(capabilities.cached(ssh_control.server(s['id']))['data'])
        except HTTPException as exc:pause(r,exc.detail);return
    step['status']='done';step['finished']=now;save_steps(r,steps,r['position']+1)


def tick():
    from . import ssh_control,agent
    c=db.get_conn()
    with agent.LOCK:
        for raw in c.execute("SELECT * FROM recovery_runs WHERE status='running' ORDER BY created LIMIT 10").fetchall():
            r=dict(raw)
            try:_advance(r)
            except HTTPException as exc:pause(r,str(exc.detail))
            except Exception as exc:pause(r,'恢复步骤未完成（'+type(exc).__name__+'），请检查任务与服务器状态')
        c.execute("DELETE FROM recovery_runs WHERE status IN ('done','cancelled') AND id NOT IN (SELECT id FROM recovery_runs ORDER BY created DESC LIMIT 100)");c.commit()


def register(app,owner):
    @app.patch('/api/recoveries/{run_id}/connection')
    def fix_connection(run_id:str,body:ConnectionFix,request:Request):
        owner(request)
        from . import agent,ssh_control
        with agent.LOCK:
            r=get(run_id);steps=json.loads(r['steps'])
            if r['status']!='paused' or r['position']>=len(steps) or steps[r['position']]['key']!='forwarding':raise HTTPException(409,'仅可在转发验证暂停时修正客户端连接参数')
            if ssh_control.busy(r['server_id']):raise HTTPException(409,'请先处理当前 SSH 任务')
            nid=steps[r['position']]['node_id'];a=agent.row(nid);n=db.get_node(nid)
            if not a or not n:raise HTTPException(409,'节点已不存在')
            item=db.get_conn().execute('SELECT * FROM recovery_nodes WHERE run_id=? AND node_id=?',(run_id,nid)).fetchone()
            if not item or a['applied_revision']!=item['expected_revision'] or json.loads(a['desired_json'])!=json.loads(item['config']):raise HTTPException(409,'节点安装版本或保存配置已变化，请先核对实际状态')
            if db.get_conn().execute("SELECT 1 FROM agent_jobs j JOIN ssh_nodes b ON b.node_id=j.node_id WHERE b.server_id=? AND j.status IN ('queued','running')",(r['server_id'],)).fetchone():raise HTTPException(409,'同机有未完成配置任务，请等待完成')
            draft=agent.AgentEditBody(remark=n['remark'],node_port=a['actual_port'],client_address=n['client_address'] if body.client_address is None else body.client_address,client_port=body.client_port)
            agent.edit(nid,draft,allowed_recovery=run_id)
            db.get_conn().execute('UPDATE recovery_nodes SET config=? WHERE run_id=? AND node_id=?',(agent.row(nid)['desired_json'],run_id,nid));db.get_conn().commit()
            return public(get(run_id))

    @app.get('/api/recoveries')
    def listing(request:Request):
        owner(request);return [public(dict(r)) for r in db.get_conn().execute('SELECT * FROM recovery_runs ORDER BY created DESC LIMIT 50')]

    @app.post('/api/servers/{server_id}/recoveries')
    def start(server_id:str,body:Start,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:return create(server_id,body)

    @app.get('/api/recoveries/{run_id}')
    def details(run_id:str,request:Request):owner(request);return public(get(run_id))

    @app.post('/api/recoveries/{run_id}/resume')
    def resume(run_id:str,request:Request):
        owner(request)
        from . import ssh_control,agent
        with agent.LOCK:
            r=get(run_id)
            if r['status']!='paused' or ssh_control.busy(r['server_id']):raise HTTPException(409,'先处理未完成或结果未知的 SSH 任务')
            steps=json.loads(r['steps']);step=steps[r['position']]
            if step.get('status')=='failed' or step['key']=='preflight':
                step.pop('task_id',None);step.pop('started',None);step['status']='pending'
            elif step['key'] in ('forwarding','sampling'):
                step.pop('started',None);step.pop('checked',None);step.pop('probe_requested',None)
            c=db.get_conn();c.execute("UPDATE recovery_runs SET status='running',deadline=?,connection=?,steps=?,error='' WHERE id=?",(time.time()+7200,connection(ssh_control.server(r['server_id'])),json.dumps(steps),run_id));c.commit()
            return public(get(run_id))

    @app.post('/api/recoveries/{run_id}/cancel')
    def cancel(run_id:str,request:Request):
        owner(request)
        from . import ssh_control,agent
        with agent.LOCK:
            r=get(run_id)
            if r['status'] not in ('running','paused'):raise HTTPException(409,'此流程已经结束')
            if ssh_control.busy(r['server_id']):raise HTTPException(409,'请先等待或处理当前 SSH 步骤；取消不会强杀远端任务')
            if db.get_conn().execute("SELECT 1 FROM agent_jobs j JOIN ssh_nodes n ON n.node_id=j.node_id WHERE n.server_id=? AND j.status='running'",(r['server_id'],)).fetchone():raise HTTPException(409,'同机仍有执行中的协议任务，请先检查任务结果')
            for n in db.get_conn().execute('SELECT * FROM recovery_nodes WHERE run_id=?',(run_id,)).fetchall():
                if agent.row(n['node_id'])['bound']:
                    db.get_conn().execute("UPDATE agent_jobs SET status='failed',finished_at=?,error='服务器恢复流程取消了原排队任务' WHERE node_id=? AND revision=? AND status='queued'",(db.utcnow(),n['node_id'],n['expected_revision']))
                db.get_conn().execute('UPDATE node_agents SET enrollment_hash=NULL,enrollment_until=0,enrollment_reinstall=0 WHERE node_id=? AND enrollment_hash=?',(n['node_id'],n['enrollment_hash']))
            db.get_conn().commit()
            complete(r,'cancelled');return public(get(run_id))
