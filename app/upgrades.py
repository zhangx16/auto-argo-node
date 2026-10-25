"""Persistent, serial canary rollouts over existing jobs and forwarding probes."""
import hashlib
import json
import time
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from . import db, operations, events


def init():
    db.get_conn().executescript('''
    CREATE TABLE IF NOT EXISTS upgrade_batches(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,
      status TEXT NOT NULL,created_at TEXT NOT NULL,fingerprint TEXT NOT NULL,message TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS upgrade_items(batch_id INTEGER NOT NULL REFERENCES upgrade_batches(id) ON DELETE CASCADE,
      position INTEGER NOT NULL,node_id INTEGER NOT NULL,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
      revision INTEGER,started REAL NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT '',PRIMARY KEY(batch_id,position));
    ''');db.get_conn().commit()
    # Clean transient fields written by early rollouts; retain pins in agent_jobs.
    c=db.get_conn()
    for r in c.execute('SELECT node_id,desired_json FROM node_agents').fetchall():
        cfg=json.loads(r['desired_json'])
        if '_upgrade_hashes' in cfg or '_release_sha256' in cfg:
            cfg.pop('_upgrade_hashes',None);cfg.pop('_release_sha256',None)
            c.execute('UPDATE node_agents SET desired_json=? WHERE node_id=?',(json.dumps(cfg),r['node_id']))
    c.commit()


def asset_hashes():
    from . import agent, deploy
    from . import artifact_cache
    names=['agent','repair','fixed','traffic','resources','diagnose','core']
    hashes={name+'.sh':artifact_cache.digest(agent.ROOT/('node-'+name+'.sh')) for name in names}
    hashes['library.sh']=hashlib.sha256((deploy.installer_text().rsplit('\nmain',1)[0]+'\n').encode()).hexdigest()
    return hashes


def fingerprint(kind):
    from . import monitor
    try:
        return json.dumps(asset_hashes(),sort_keys=True) if kind=='agent' else hashlib.sha256((monitor.RELEASES/'manifest.json').read_bytes()).hexdigest()
    except (OSError,ValueError):raise HTTPException(409,'升级资源尚未准备好')


def host_busy(node_id):
    c=db.get_conn()
    return bool(c.execute('''SELECT 1 FROM agent_jobs j WHERE j.status IN ('queued','running') AND
      (j.node_id=? OR j.node_id IN (SELECT node_id FROM monitor_nodes WHERE host_id IN
      (SELECT host_id FROM monitor_nodes WHERE node_id=?)))''',(node_id,node_id)).fetchone())


def inventory():
    from . import agent,monitor
    c=db.get_conn();nodes=[]
    for n in c.execute('SELECT n.id,n.remark,n.status,a.* FROM nodes n JOIN node_agents a ON a.node_id=n.id ORDER BY n.id'):
        n=dict(n);host=c.execute('SELECT h.id,h.name,h.health FROM monitor_hosts h JOIN monitor_nodes m ON h.id=m.host_id WHERE m.node_id=?',(n['id'],)).fetchone()
        core={}
        if host:
            for s in json.loads(host['health']).get('services',[]):
                if s.get('node_id')==n['id']:core=s.get('core') or {}
        nodes.append({'id':n['id'],'name':n['remark'],'host_id':host['id'] if host else '',
            'host_name':host['name'] if host else '', 'agent_version':n['agent_version'],
            'core_channel':n['core_channel'],'core_version':n['core_version'] or str(core.get('version',''))[:64], 'probe_sha256':n['probe_sha256'],
            'core_sha256':n['core_sha256'] or str(core.get('sha256',''))[:64],
            'eligible':bool(n['bound'] and n['management_enabled'] and n['status']!='uninstalling'),
            'fresh':min(agent.age(n['last_report_at']),agent.age(n['last_management_at']))<180,'control':operations.control(n['id'])})
    try:
        manifest=json.loads((monitor.RELEASES/'manifest.json').read_text())
        release={k:manifest.get(k) for k in ('core_version','monitor_version')}
    except (OSError,ValueError):release={}
    hosts=[{'id':r['id'],'name':r['name'],'maintenance':operations.host_maintenance(r['id'])} for r in c.execute('SELECT id,name FROM monitor_hosts ORDER BY name')]
    return {'nodes':nodes,'hosts':hosts,'target_agent_version':operations.AGENT_VERSION,'release':release}


class BatchBody(BaseModel):
    kind:Literal['agent','minimal','original']='agent'
    node_ids:list[int]=Field(min_length=1,max_length=100)


def create(body):
    from . import agent,changes
    c=db.get_conn()
    if len(set(body.node_ids))!=len(body.node_ids):raise HTTPException(400,'节点重复')
    if c.execute("SELECT 1 FROM upgrade_batches WHERE status IN ('running','paused')").fetchone():raise HTTPException(409,'请先完成或取消已有升级批次')
    names=[];hosts=set()
    for node_id in body.node_ids:
        n=db.get_node(node_id);a=agent.row(node_id)
        if not n or not a or not a['bound'] or not a['management_enabled'] or n['status']=='uninstalling':raise HTTPException(409,f'节点 {node_id} 未启用管理或正在卸载')
        if operations.maintenance(node_id) or host_busy(node_id):raise HTTPException(409,f'节点 {node_id} 正在维护或有未完成任务')
        if a['probe_status']!='ok' or agent.age(a['probe_at'])>=600:raise HTTPException(409,f'节点 {node_id} 缺少最近 10 分钟成功转发基线，请先测活')
        changes.guard(node_id)
        h=c.execute('SELECT host_id FROM monitor_nodes WHERE node_id=?',(node_id,)).fetchone()
        host=h['host_id'] if h else None
        if body.kind=='monitor' and not host:raise HTTPException(409,'请先为所选节点绑定资源探针')
        if host and host in hosts:raise HTTPException(409,'同一服务器共享核心与脚本，请每台只选一个代表节点')
        if host:hosts.add(host)
        names.append(n['remark'])
    frozen=fingerprint(body.kind)
    cur=c.execute("INSERT INTO upgrade_batches(kind,status,created_at,fingerprint) VALUES (?,'running',?,?)",(body.kind,db.utcnow(),frozen))
    batch_id=cur.lastrowid
    for pos,(node_id,name) in enumerate(zip(body.node_ids,names)):
        c.execute('INSERT INTO upgrade_items(batch_id,position,node_id,name) VALUES (?,?,?,?)',(batch_id,pos,node_id,name))
        events.add(node_id,'upgrade','已加入升级批次；首台验证通过后依次推进')
    c.commit();return {'id':batch_id}


def pause(batch_id,message):
    c=db.get_conn();c.execute("UPDATE upgrade_batches SET status='paused',message=? WHERE id=?",(message,batch_id));c.commit()


def tick():
    from . import agent,monitor,changes,ssh_control
    with agent.LOCK:
        c=db.get_conn()
        c.execute("DELETE FROM upgrade_batches WHERE status IN ('done','cancelled') AND id NOT IN (SELECT id FROM upgrade_batches ORDER BY id DESC LIMIT 200)");c.commit()
        b=c.execute("SELECT * FROM upgrade_batches WHERE status='running' ORDER BY id LIMIT 1").fetchone()
        if not b:return
        i=c.execute("SELECT * FROM upgrade_items WHERE batch_id=? AND status IN ('pending','running','verifying') ORDER BY position LIMIT 1",(b['id'],)).fetchone()
        if not i:
            c.execute("UPDATE upgrade_batches SET status='done',message='所选节点升级并通过转发验证' WHERE id=?",(b['id'],));c.commit();return
        n=db.get_node(i['node_id']);a=agent.row(i['node_id'])
        if not n or not a:pause(b['id'],'节点已删除，后续升级暂停');return
        if operations.maintenance(i['node_id']):pause(b['id'],'节点处于维护模式，后续升级暂停');return
        if i['status']=='pending':
            try:current=fingerprint(b['kind'])
            except HTTPException:pause(b['id'],'升级资源不可用，请检查发布文件');return
            if current!=b['fingerprint']:pause(b['id'],'升级资源发生变化，请取消并重新建立批次');return
            if host_busy(i['node_id']):pause(b['id'],'同主机有其他任务，请完成后继续');return
            unmonitored_ssh=a['health_mode']=='none' and ssh_control.controls(i['node_id'])
            if not a['management_enabled'] or (not unmonitored_ssh and min(agent.age(a['last_report_at']),agent.age(a['last_management_at']))>=180):
                pause(b['id'],'节点不在线或管理已关闭，请检查后继续');return
            if a['probe_status'] in ('pending','unknown') or agent.age(a['probe_at'])>=600:
                agent.schedule_probe(i['node_id']);return
            if a['probe_status']!='ok':pause(b['id'],'升级前实际转发未通过，请检查后继续');return
            try:changes.guard(i['node_id'])
            except HTTPException:pause(b['id'],'节点有配置验证尚未完成');return
            cfg=json.loads(a['desired_json']);cfg['node_port']=a['actual_port']
            if b['kind']=='agent':cfg['_upgrade_hashes']=json.loads(b['fingerprint'])
            else:cfg.update(core_channel=b['kind'],_release_sha256=b['fingerprint'])
            if b['kind']=='monitor':
                host=c.execute('SELECT host_id FROM monitor_nodes WHERE node_id=?',(i['node_id'],)).fetchone()
                if not host:pause(b['id'],'资源探针绑定已移除');return
                cfg['monitor_host_id']=host['host_id']
            rev=agent.enqueue(i['node_id'],'upgrade' if b['kind']=='agent' else 'monitor_install' if b['kind']=='monitor' else 'core',cfg)
            if b['kind'] in ('minimal','original'):c.execute('INSERT OR REPLACE INTO core_rollouts VALUES (?,?,?,?,?)',(i['node_id'],rev,'applying',1,a['core_channel']))
            c.execute("UPDATE upgrade_items SET status='running',revision=?,started=? WHERE batch_id=? AND position=?",(rev,time.time(),b['id'],i['position']));c.commit();return
        job=c.execute('SELECT * FROM agent_jobs WHERE node_id=? AND revision=?',(i['node_id'],i['revision'])).fetchone()
        failed=not job or job['status']=='failed' or time.time()-i['started']>2100
        if not failed and job['status']!='done':return
        if not failed and a['applied_revision']!=i['revision']:failed=True
        if failed:
            c.execute("UPDATE upgrade_items SET status='failed',message='任务失败、配置版本已变化或超过 35 分钟未完成' WHERE batch_id=? AND position=?",(b['id'],i['position']))
            events.add(i['node_id'],'upgrade','升级批次暂停：任务失败或超时，请查看故障详情','warning')
            pause(b['id'],'升级失败，后续节点未执行');return
        if i['status']=='running':
            c.execute("UPDATE upgrade_items SET status='verifying' WHERE batch_id=? AND position=?",(b['id'],i['position']));c.commit()
            agent.schedule_probe(i['node_id']);return
        if a['probe_at']<=job['finished_at'] or a['probe_status'] in ('pending','unknown'):
            agent.schedule_probe(i['node_id']);return
        if b['kind']=='monitor':
            host=c.execute('SELECT h.last_seen FROM monitor_hosts h JOIN monitor_nodes m ON h.id=m.host_id WHERE m.node_id=?',(i['node_id'],)).fetchone()
            if not host or time.time()-host['last_seen']>90 or host['last_seen']<=i['started']:return
        if b['kind']=='agent' and a['agent_version']<operations.AGENT_VERSION:return
        good=a['probe_status']=='ok'
        c.execute('UPDATE upgrade_items SET status=?,message=? WHERE batch_id=? AND position=?',('done' if good else 'failed','升级后实际转发验证通过' if good else '升级后实际转发失败',b['id'],i['position']))
        events.add(i['node_id'],'upgrade','升级并通过实际转发验证' if good else '升级后转发验证失败；后续批次暂停','info' if good else 'warning');c.commit()
        if not good:pause(b['id'],'转发验证失败；核心升级按原有策略回滚，请核对回滚结果')


def batches():
    c=db.get_conn();result=[]
    for r in c.execute('SELECT id,kind,status,created_at,message FROM upgrade_batches ORDER BY id DESC LIMIT 30').fetchall():
        b=dict(r);b['items']=[dict(i) for i in c.execute('SELECT position,node_id,name,status,revision,message FROM upgrade_items WHERE batch_id=? ORDER BY position',(r['id'],))];result.append(b)
    return result


def register(app,owner):
    @app.get('/api/operations/versions')
    def versions(request:Request):owner(request);return inventory()

    @app.get('/api/operations/upgrades')
    def listing(request:Request):owner(request);return batches()

    @app.post('/api/operations/upgrades')
    def start(body:BatchBody,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:return create(body)

    @app.post('/api/operations/upgrades/{batch_id}/{action}')
    def action(batch_id:int,action:Literal['pause','resume','cancel'],request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:
            c=db.get_conn();b=c.execute('SELECT * FROM upgrade_batches WHERE id=?',(batch_id,)).fetchone()
            if not b:raise HTTPException(404,'批次不存在')
            if b['status'] in ('done','cancelled'):raise HTTPException(409,'批次已结束')
            if action=='resume' and c.execute("SELECT 1 FROM upgrade_items WHERE batch_id=? AND status='failed'",(batch_id,)).fetchone():raise HTTPException(409,'失败批次请取消，处理故障后重新选择节点创建；不会跳过失败节点')
            state={'pause':'paused','resume':'running','cancel':'cancelled'}[action]
            c.execute('UPDATE upgrade_batches SET status=?,message=? WHERE id=?',(state,'管理员操作：'+action+'；已下发任务继续执行',batch_id));c.commit()
            return {'ok':True}
