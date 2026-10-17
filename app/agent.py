"""One-time enrollment, scoped node credentials, ordered jobs and health reports."""
import hashlib
import ipaddress
import json
import secrets
import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

from fastapi import HTTPException, Request, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from . import db, deploy

ROOT=Path(__file__).parent
LOCK=deploy.START_LOCK
PROBE_LOCK=threading.Lock()
PROBING=set()
STOP=threading.Event()
LOOP=None
PUBLIC_URL=None


def init():
    c=db.get_conn()
    c.executescript('''CREATE TABLE IF NOT EXISTS uninstall_receipts (token_hash TEXT PRIMARY KEY, revision INTEGER NOT NULL, expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS node_agents (
        node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
        token_hash TEXT UNIQUE, enrollment_hash TEXT UNIQUE, enrollment_until INTEGER NOT NULL DEFAULT 0,
        bound INTEGER NOT NULL DEFAULT 0, desired_revision INTEGER NOT NULL DEFAULT 1,
        applied_revision INTEGER NOT NULL DEFAULT 0, desired_json TEXT NOT NULL,
        actual_port INTEGER NOT NULL, last_report_at TEXT NOT NULL DEFAULT '',
        health_status TEXT NOT NULL DEFAULT 'waiting', process_alive INTEGER NOT NULL DEFAULT 0,
        local_ws INTEGER NOT NULL DEFAULT 0, public_ws INTEGER NOT NULL DEFAULT 0,
        failure_count INTEGER NOT NULL DEFAULT 0, last_repair INTEGER NOT NULL DEFAULT 0,
        repair_count INTEGER NOT NULL DEFAULT 0, probe_status TEXT NOT NULL DEFAULT 'unknown',
        probe_at TEXT NOT NULL DEFAULT '', probe_error TEXT NOT NULL DEFAULT '', latency_ms INTEGER,
        last_job_error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS agent_jobs (
        node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, operation TEXT NOT NULL, config_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(node_id,revision));''')
    c.commit()


def row(node_id):
    # Also safe when legacy unit tests initialize only the SSH schema.
    if not db.get_conn().execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_agents'").fetchone():return None
    r=db.get_conn().execute('SELECT * FROM node_agents WHERE node_id=?',(node_id,)).fetchone()
    return dict(r) if r else None


def age(stamp):
    if not stamp:return float('inf')
    return max(0,(datetime.now(timezone.utc)-datetime.strptime(stamp,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)).total_seconds())


def enrich(node):
    a=row(node['id'])
    if not a:return node
    node.update(management='agent',agent_bound=bool(a['bound']),health_status=a['health_status'],
                last_report_at=a['last_report_at'],process_alive=bool(a['process_alive']),local_ws=bool(a['local_ws']),
                public_ws=bool(a['public_ws']),failure_count=a['failure_count'],repair_count=a['repair_count'],
                probe_status=a['probe_status'],probe_at=a['probe_at'],probe_error=a['probe_error'],latency_ms=a['latency_ms'],
                desired_port=json.loads(a['desired_json'])['node_port'],desired_revision=a['desired_revision'],applied_revision=a['applied_revision'])
    job=db.get_conn().execute("SELECT status FROM agent_jobs WHERE node_id=? AND revision=?",(node['id'],a['desired_revision'])).fetchone()
    node['job_status']=job['status'] if job else 'done'
    if db.get_node(node['id'])['status']=='uninstalling':node['status']='uninstalling';node['deleting']=True
    if a['bound'] and age(a['last_report_at'])>180:
        node['health_status']='offline'
        node['status']='offline'
    node.pop('install_cmd',None);node.pop('uninstall_cmd',None)
    return node


class CreateBody(deploy.NodeEditBody):
    ws_path: str=Field(default='',max_length=128)
    uuid: str=Field(default='',max_length=36)
    @field_validator('ws_path')
    @classmethod
    def path_valid(cls,v):return deploy.DeployBody.path_valid(v)
    @field_validator('uuid')
    @classmethod
    def uuid_valid(cls,v):return deploy.DeployBody.uuid_valid(v)

class EnrollBody(BaseModel):
    enrollment_token: str=Field(min_length=20,max_length=128)
    hostname: str=Field(default='',max_length=253)
    os: str=Field(default='',max_length=100)
    arch: str=Field(default='',max_length=32)
    ip: str=Field(default='',max_length=64)

class Metadata(BaseModel):
    domain: str=Field(pattern=r'^[a-z0-9-]+\.trycloudflare\.com$',max_length=253)
    uuid: str=Field(max_length=36)
    path: str=Field(min_length=1,max_length=512)
    local_port: int=Field(ge=1024,le=65535)
    @field_validator('uuid')
    @classmethod
    def uuid_valid(cls,v):return deploy.DeployBody.uuid_valid(v)

class Report(BaseModel):
    state: str=Field(pattern=r'^(waiting|installing|online|degraded|network_error|repairing|error)$')
    metadata: Metadata | None=None
    process_alive: bool=False
    local_ws: bool=False
    public_ws: bool=False
    failure_count: int=Field(default=0,ge=0,le=100000)
    last_repair: int=Field(default=0,ge=0)
    repair_count: int=Field(default=0,ge=0)
    job_revision: int | None=Field(default=None,ge=1)
    job_ok: bool | None=None
    error: str=Field(default='',max_length=2000)
    logs: list[str]=Field(default_factory=list,max_length=40)
    @field_validator('logs')
    @classmethod
    def log_size(cls,v):return [x[:1000] for x in v]


def digest(token):return hashlib.sha256(token.encode()).hexdigest()


def ensure_node(node_id):
    node=db.get_node(node_id)
    if not node:raise HTTPException(404,'节点不存在')
    n=deploy.enrich(node)
    if n.get('protocol')!='vless-ws-argo':raise HTTPException(400,'仅支持接入 VLESS-WS + Argo 节点')
    a=row(node_id)
    if a:return a
    if node['status']=='installing':raise HTTPException(409,'SSH 部署仍在运行，请等待结束后接入')
    path=n.get('ws_path') or parse_qs(urlparse(node['share_link']).query).get('path',[''])[0].split('?',1)[0] or '/'+secrets.token_hex(12)
    config={'node_port':node['node_port'],'ws_path':path,'uuid':node['uuid'] or str(uuid4())}
    c=db.get_conn()
    c.execute('INSERT INTO node_agents(node_id,desired_json,actual_port) VALUES (?,?,?)',(node_id,json.dumps(config),node['node_port']))
    operation='read' if node['share_link'] else 'install'
    c.execute('INSERT INTO agent_jobs(node_id,revision,operation,config_json,created_at) VALUES (?,1,?,?,?)',(node_id,operation,json.dumps(config),db.utcnow()))
    c.commit()
    return row(node_id)


def enrollment(node_id):
    ensure_node(node_id)
    token=secrets.token_urlsafe(32)
    c=db.get_conn();c.execute('UPDATE node_agents SET enrollment_hash=?,enrollment_until=? WHERE node_id=?',(digest(token),int(time.time())+86400,node_id));c.commit()
    url=PUBLIC_URL()+'/agent/bootstrap/'+token+'.sh'
    # POSIX sh works on Alpine even before bash is installed. No credential in the returned command except the scoped, expiring enrollment token.
    script='umask 077; f=$(mktemp); trap \'rm -f "$f"\' EXIT; (if command -v curl >/dev/null 2>&1; then curl -fsSL '+shlex.quote(url)+' -o "$f" || wget -q '+shlex.quote(url)+' -O "$f"; else wget -q '+shlex.quote(url)+' -O "$f"; fi) && sh "$f"'
    return {'id':node_id,'command':'sh -c '+shlex.quote(script),'expires_in':86400,'agent_bound':bool(row(node_id)['bound'])}


def enqueue(node_id,operation,config):
    a=row(node_id);revision=a['desired_revision']+1;c=db.get_conn()
    c.execute('INSERT INTO agent_jobs(node_id,revision,operation,config_json,created_at) VALUES (?,?,?,?,?)',(node_id,revision,operation,json.dumps(config),db.utcnow()))
    c.execute('UPDATE node_agents SET desired_revision=?,desired_json=?,last_job_error=\'\' WHERE node_id=?',(revision,json.dumps(config),node_id));c.commit()
    db.update_node(node_id,status='installing' if a['bound'] else 'pending',last_error='')
    db.add_log(node_id,'已排队配置任务，等待节点下一次连接面板')
    return revision


def edit(node_id,body):
    if db.get_node(node_id) and db.get_node(node_id)['status']=='uninstalling':raise HTTPException(409,'节点正在等待卸载')
    a=ensure_node(node_id);n=db.get_node(node_id)
    running=db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone()
    desired=json.loads(a['desired_json'])
    if body.node_port!=desired['node_port']:
        desired['node_port']=body.node_port
        if not a['bound'] and not n['share_link'] and a['applied_revision']==0:
            c=db.get_conn();c.execute('UPDATE node_agents SET desired_json=?,actual_port=? WHERE node_id=?',(json.dumps(desired),body.node_port,node_id));c.execute("UPDATE agent_jobs SET config_json=? WHERE node_id=? AND status='queued'",(json.dumps(desired),node_id));c.commit()
            db.update_node(node_id,node_port=body.node_port)
        else:
            if running:raise HTTPException(409,'请先完成当前安装或配置任务，再修改监听端口')
            enqueue(node_id,'update' if n['share_link'] else 'install',desired)
    # Rebuild the client link immediately using the actual, currently applied metadata.
    link=n['share_link']
    if link:
        d=deploy.enrich(n)
        path=d['ws_path'] or parse_qs(urlparse(link).query).get('path',[''])[0]
        link=deploy.make_link({'domain':n['sni'],'uuid':n['uuid'],'path':path},body)
    db.update_node(node_id,remark=body.remark,client_address=body.client_address,share_link=link)
    c=db.get_conn();c.execute('UPDATE deployments SET client_port=? WHERE node_id=?',(body.client_port,node_id))
    if link!=n['share_link']:c.execute("UPDATE node_agents SET probe_status='unknown',probe_at='',probe_error='',latency_ms=NULL WHERE node_id=?",(node_id,))
    c.commit()
    if link!=n['share_link'] and a['bound']:schedule_probe(node_id)
    db.add_log(node_id,'面板参数已保存；远端端口以节点确认执行后的结果为准')
    return enrich(deploy.enrich(db.get_node(node_id)))


def authorize(token):
    if not token:raise HTTPException(401,'缺少节点凭据')
    a=db.get_conn().execute('SELECT * FROM node_agents WHERE token_hash=? AND bound=1',(digest(token),)).fetchone()
    if not a:raise HTTPException(401,'节点凭据已失效')
    return dict(a)


def accept_report(a,report):
    node_id=a['node_id'];node=db.get_node(node_id)
    if not node:raise HTTPException(401,'节点记录已删除')
    if node['status']=='uninstalling':
        if report.job_ok is False:
            db.update_node(node_id,last_error=report.error or '卸载未完成，等待重试')
            db.add_log(node_id,'卸载未完成，将在下次连接重试：'+report.error)
            for line in report.logs:db.add_log(node_id,line)
        db.get_conn().execute('UPDATE node_agents SET last_report_at=? WHERE node_id=?',(db.utcnow(),node_id));db.get_conn().commit()
        return {'ok':True,'applied_revision':a['applied_revision']}
    metadata=report.metadata
    job=None
    if report.job_revision is not None and report.job_ok is not None:
        job=db.get_conn().execute('SELECT * FROM agent_jobs WHERE node_id=? AND revision=?',(node_id,report.job_revision)).fetchone()
        if not job:raise HTTPException(409,'未知配置版本')
        if report.job_revision<=a['applied_revision'] or job['status']=='failed':
            return {'ok':True,'applied_revision':a['applied_revision']}
    expected=json.loads(job['config_json'] if job and report.job_ok else a['desired_json'])['uuid']
    if metadata and metadata.uuid!=expected:raise HTTPException(409,'节点 UUID 与绑定记录不一致')
    if job and report.job_ok:
        if metadata is None or metadata.local_port!=json.loads(job['config_json'])['node_port']:
            raise HTTPException(409,'执行结果的监听端口与配置版本不一致')
    elif metadata and metadata.local_port!=a['actual_port']:
        raise HTTPException(409,'当前上报端口与已应用配置不一致')
    c=db.get_conn();old_domain=node['sni'];changed=False
    try:
        c.execute('UPDATE node_agents SET last_report_at=?,health_status=?,process_alive=?,local_ws=?,public_ws=?,failure_count=?,last_repair=?,repair_count=? WHERE node_id=?',
                  (db.utcnow(),report.state,int(report.process_alive),int(report.local_ws),int(report.public_ws),report.failure_count,report.last_repair,report.repair_count,node_id))
        if job:
            state='done' if report.job_ok else 'failed'
            c.execute('UPDATE agent_jobs SET status=?,finished_at=? WHERE node_id=? AND revision=?',(state,db.utcnow(),node_id,report.job_revision))
            if report.job_ok:
                c.execute('UPDATE node_agents SET applied_revision=?,actual_port=?,last_job_error=\'\' WHERE node_id=?',(report.job_revision,metadata.local_port,node_id))
            else:
                c.execute('UPDATE node_agents SET last_job_error=? WHERE node_id=?',(report.error or '节点任务执行失败',node_id))
        if metadata:
            body=deploy.NodeEditBody(remark=node['remark'],node_port=metadata.local_port,client_address=node['client_address'],client_port=db.get_conn().execute('SELECT client_port FROM deployments WHERE node_id=?',(node_id,)).fetchone()['client_port'])
            link=deploy.make_link(metadata.model_dump(),body)
            changed=link!=node['share_link']
            c.execute('UPDATE nodes SET share_link=?,uuid=?,sni=?,node_port=? WHERE id=?',(link,metadata.uuid,metadata.domain,metadata.local_port,node_id))
            c.execute('UPDATE deployments SET ws_path=? WHERE node_id=?',(metadata.path,node_id))
            if changed:c.execute("UPDATE node_agents SET probe_status='unknown',probe_at='',probe_error='',latency_ms=NULL WHERE node_id=?",(node_id,))
        c.commit()
    except Exception:c.rollback();raise
    latest=row(node_id)
    pending=c.execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone()
    error=latest['last_job_error']
    status='installing' if pending else ('error' if error else ('online' if report.local_ws and report.public_ws else 'degraded'))
    if not metadata and not node['share_link'] and not pending:status='error'
    db.update_node(node_id,status=status,last_seen=db.utcnow(),last_error=error or report.error)
    if changed:db.add_log(node_id,'已同步节点最新域名和 VLESS 链接'+('（临时隧道域名已变化）' if old_domain and old_domain!=metadata.domain else ''))
    if report.repair_count>a['repair_count']:
        db.add_log(node_id,f'节点已尝试第 {report.repair_count} 次自动修复，当前状态：{report.state}')
    if job:
        db.add_log(node_id,f'配置版本 {report.job_revision} '+('执行成功' if report.job_ok else '执行失败：'+report.error))
        for line in report.logs:db.add_log(node_id,line)
    if changed and metadata and report.state!='installing':schedule_probe(node_id)
    return {'ok':True,'applied_revision':latest['applied_revision']}


def job_script(a,job):
    config=json.loads(job['config_json']);n=deploy.enrich(db.get_node(a['node_id']))
    if job['operation']=='uninstall':
        return (ROOT/'node-uninstall.sh').read_text().replace('__NODE_ID__',str(a['node_id'])).replace('__UUID__',shlex.quote(config['uuid'])).replace('__REVISION__',str(job['revision']))
    body=deploy.DeployBody(host='agent.local',host_key='agent-generated-placeholder-key',operation=job['operation'],
                          old_port=a['actual_port'],expected_uuid=config['uuid'],node_port=config['node_port'],ws_path=config['ws_path'],uuid=config['uuid'],
                          remark=n['remark'],client_address=n['client_address'],client_port=n['client_port'])
    script=deploy.driver(body)
    if job['operation'] in ('install','update'):
        tag='argo-vless-ws-'+str(config['node_port'])
        # Recovery after a lost callback must not install a second node or migrate twice.
        prelude="if command -v jq >/dev/null && [ -s /usr/local/etc/sing-box/argo_metadata.json ] && jq -e --arg t "+shlex.quote(tag)+" --arg u "+shlex.quote(config['uuid'])+" '.[$t] | select(.uuid == $u and .type == \"temp\")' /usr/local/etc/sing-box/argo_metadata.json >/dev/null 2>&1; then\n printf 'SB_WEB_RESULT='\n jq -c --arg t "+shlex.quote(tag)+" '.[$t]' /usr/local/etc/sing-box/argo_metadata.json\n exit 0\nfi\n"
        script=script.replace('set -eu\n','set -eu\n'+prelude,1)
    return script


def request_delete(node):
    a=row(node['id'])
    if not a or not a['bound']:
        if node['share_link']:raise HTTPException(409,'请先接入监控，执行接入命令后才能从面板卸载远端节点')
        db.delete_node(node['id']);return {'ok':True,'deleted':True}
    if node['status']=='uninstalling':return {'ok':True,'pending':True}
    c=db.get_conn()
    c.execute("UPDATE agent_jobs SET status='failed',finished_at=? WHERE node_id=? AND status IN ('queued','running')",(db.utcnow(),node['id']));c.commit()
    config=json.loads(a['desired_json']);config['node_port']=a['actual_port']
    enqueue(node['id'],'uninstall',config)
    db.update_node(node['id'],status='uninstalling')
    db.add_log(node['id'],'已排队远端卸载，收到清理成功确认后自动删除记录')
    return {'ok':True,'pending':True}


def deletion_blocked(node):
    a=row(node['id'])
    if not a:return node['status']=='installing' and bool(db.get_conn().execute('SELECT 1 FROM deployments WHERE node_id=?',(node['id'],)).fetchone())
    return age(a['last_report_at'])<180 and bool(db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status='running'",(node['id'],)).fetchone())


def schedule_probe(node_id):
    with PROBE_LOCK:
        if node_id in PROBING:return False
        if len(PROBING)>=2:return False
        n=db.get_node(node_id)
        if not n or not n['share_link']:return False
        PROBING.add(node_id)
    c=db.get_conn();c.execute("UPDATE node_agents SET probe_status='pending' WHERE node_id=?",(node_id,));c.commit()
    def run():
        try:
            from .node_probe import probe_link
            result=probe_link(n['share_link']);state='ok';error='';latency=result['latency_ms']
        except Exception as e:state='failed';error=(type(e).__name__+': '+str(e))[:300];latency=None
        try:
            conn=db.get_conn()
            conn.execute('UPDATE node_agents SET probe_status=?,probe_at=?,probe_error=?,latency_ms=? WHERE node_id=? AND EXISTS (SELECT 1 FROM nodes WHERE id=? AND share_link=?)',(state,db.utcnow(),error,latency,node_id,node_id,n['share_link']));conn.commit()
        finally:
            with PROBE_LOCK:PROBING.discard(node_id)
    threading.Thread(target=run,daemon=True).start();return True


def start_loop():
    global LOOP
    if LOOP and LOOP.is_alive():return
    STOP.clear()
    def loop():
        while not STOP.wait(30):
            try:
                for a in db.get_conn().execute('SELECT * FROM node_agents WHERE bound=1').fetchall():
                    if age(a['last_report_at'])<180 and (age(a['probe_at'])>=300 or a['probe_status']=='pending'):
                        schedule_probe(a['node_id'])
            except Exception:pass
    LOOP=threading.Thread(target=loop,daemon=True);LOOP.start()


def register(app,require_login,public_url):
    global PUBLIC_URL
    PUBLIC_URL=public_url
    def owner(request):
        require_login(request)
        if request.headers.get('origin') and urlparse(request.headers['origin']).netloc!=request.headers.get('host'):
            raise HTTPException(403,'请求来源不匹配')
    @app.post('/api/agent-nodes')
    def create(body:CreateBody,request:Request):
        owner(request)
        with LOCK:
            n=db.create_node(body.remark,body.node_port,'',body.client_address,secrets.token_urlsafe(24));config={'node_port':body.node_port,'ws_path':body.ws_path or '/'+secrets.token_hex(12),'uuid':body.uuid or str(uuid4())}
            c=db.get_conn();c.execute('INSERT INTO deployments VALUES (?,?,?,?,?,?)',(n['id'],'',22,body.client_port,config['ws_path'],''));c.commit()
            db.update_node(n['id'],uuid=config['uuid'])
            ensure_node(n['id']);c.execute('UPDATE node_agents SET desired_json=? WHERE node_id=?',(json.dumps(config),n['id']));c.execute('UPDATE agent_jobs SET config_json=? WHERE node_id=? AND revision=1',(json.dumps(config),n['id']));c.commit()
            db.add_log(n['id'],'安装命令已生成，请在目标服务器以 root 执行')
            return enrollment(n['id'])
    @app.post('/api/agent-nodes/{node_id}/enrollment')
    def make_enrollment(node_id:int,request:Request):
        owner(request)
        with LOCK:return enrollment(node_id)
    @app.patch('/api/agent-nodes/{node_id}')
    def edit_node(node_id:int,body:deploy.NodeEditBody,request:Request):
        owner(request)
        with LOCK:return edit(node_id,body)
    @app.post('/api/agent-nodes/{node_id}/refresh')
    def refresh(node_id:int,request:Request):
        owner(request)
        with LOCK:
            a=ensure_node(node_id)
            if db.get_node(node_id)['status']=='uninstalling':raise HTTPException(409,'节点正在等待卸载')
            if db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone():raise HTTPException(409,'已有任务等待执行')
            config=json.loads(a['desired_json'])
            if db.get_node(node_id)['share_link']:config['node_port']=a['actual_port']
            revision=enqueue(node_id,'read' if db.get_node(node_id)['share_link'] else 'install',config)
            return {'ok':True,'revision':revision,'agent_bound':bool(a['bound'])}
    @app.post('/api/agent-nodes/{node_id}/retry')
    def retry_job(node_id:int,request:Request):
        owner(request)
        with LOCK:
            a=ensure_node(node_id)
            if db.get_node(node_id)['status']=='uninstalling':raise HTTPException(409,'节点正在等待卸载')
            if db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone():raise HTTPException(409,'已有任务等待执行')
            config=json.loads(a['desired_json']);n=db.get_node(node_id)
            operation=('update' if config['node_port']!=a['actual_port'] else 'read') if n['share_link'] else 'install'
            return {'ok':True,'revision':enqueue(node_id,operation,config),'agent_bound':bool(a['bound'])}
    @app.post('/api/agent-nodes/{node_id}/probe')
    def probe_now(node_id:int,request:Request):
        owner(request)
        if not row(node_id):raise HTTPException(400,'请先接入监控脚本')
        return {'scheduled':schedule_probe(node_id)}
    @app.get('/agent/bootstrap/{token}.sh')
    def bootstrap(token:str):
        a=db.get_conn().execute('SELECT * FROM node_agents WHERE enrollment_hash=? AND enrollment_until>?',(digest(token),int(time.time()))).fetchone()
        if not a:raise HTTPException(410,'安装命令已使用或已过期，请在面板重新生成')
        from .agent_scripts import bootstrap_script
        return PlainTextResponse(bootstrap_script(public_url(),token,a['node_id']),media_type='text/x-shellscript')
    @app.get('/agent/assets/{name}')
    def asset(name:str):
        files={'agent.sh':'node-agent.sh','repair.sh':'node-repair.sh'}
        if name=='library.sh':text=deploy.SCRIPT.read_text().rsplit('\nmain',1)[0]+'\n'
        elif name in files:text=(ROOT/files[name]).read_text()
        else:raise HTTPException(404,'资源不存在')
        return PlainTextResponse(text,media_type='text/x-shellscript')
    @app.post('/agent/enroll')
    def enroll(body:EnrollBody):
        with LOCK:
            a=db.get_conn().execute('SELECT * FROM node_agents WHERE enrollment_hash=? AND enrollment_until>?',(digest(body.enrollment_token),int(time.time()))).fetchone()
            if not a:raise HTTPException(410,'安装凭据已使用或已过期')
            token=secrets.token_urlsafe(32);c=db.get_conn();c.execute("UPDATE node_agents SET token_hash=?,bound=1,enrollment_hash=NULL,enrollment_until=0,health_status='waiting',last_report_at=? WHERE node_id=?",(digest(token),db.utcnow(),a['node_id']));c.commit()
            n=db.get_node(a['node_id']);fields={'hostname':body.hostname,'os_info':(body.os+' '+body.arch).strip()}
            try:fields['host_ip']=str(ipaddress.ip_address(body.ip))
            except ValueError:
                if not n['host_ip']:fields['host_ip']=body.hostname
            db.update_node(n['id'],**fields);db.add_log(n['id'],'节点已通过一次性安装凭据绑定，开始主动上报')
            return {'node_id':n['id'],'agent_token':token,'port':a['actual_port'],'applied_revision':a['applied_revision']}
    @app.post('/agent/uninstalled/{revision}')
    def uninstalled(revision:int,x_agent_token:str|None=Header(default=None)):
        with LOCK:
            c=db.get_conn()
            c.execute('DELETE FROM uninstall_receipts WHERE expires<?',(int(time.time()),));c.commit()
            if x_agent_token and c.execute('SELECT 1 FROM uninstall_receipts WHERE token_hash=? AND revision=?',(digest(x_agent_token),revision)).fetchone():return {'ok':True}
            a=authorize(x_agent_token)
            job=db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND revision=? AND operation='uninstall' AND status='running'",(a['node_id'],revision)).fetchone()
            if not job:raise HTTPException(409,'无有效卸载任务')
            c.execute('INSERT OR REPLACE INTO uninstall_receipts VALUES (?,?,?)',(a['token_hash'],revision,int(time.time())+86400))
            db.delete_node(a['node_id'])
            return {'ok':True}
    @app.get('/agent/work')
    def work(x_agent_token:str|None=Header(default=None)):
        a=authorize(x_agent_token)
        job=db.get_conn().execute("SELECT revision,operation,config_json FROM agent_jobs WHERE node_id=? AND status IN ('queued','running') AND revision>? ORDER BY revision LIMIT 1",(a['node_id'],a['applied_revision'])).fetchone()
        return {'job':dict(job) if job else None}
    @app.get('/agent/job/{revision}.sh')
    def get_job(revision:int,x_agent_token:str|None=Header(default=None)):
        a=authorize(x_agent_token);job=db.get_conn().execute("SELECT * FROM agent_jobs WHERE node_id=? AND revision=? AND status IN ('queued','running')",(a['node_id'],revision)).fetchone()
        if not job:raise HTTPException(409,'任务已完成或已取消')
        c=db.get_conn();c.execute("UPDATE agent_jobs SET status='running' WHERE node_id=? AND revision=?",(a['node_id'],revision));c.commit()
        db.update_node(a['node_id'],status='uninstalling' if job['operation']=='uninstall' else 'installing');return PlainTextResponse(job_script(a,job),media_type='text/x-shellscript')
    @app.post('/agent/report')
    def report(body:Report,x_agent_token:str|None=Header(default=None)):
        with LOCK:return accept_report(authorize(x_agent_token),body)
