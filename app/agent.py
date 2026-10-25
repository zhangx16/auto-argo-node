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
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException, Request, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from .resources import Sample as ResourceSample
from . import db, deploy, cloudflare, direct_nodes, telemetry, regions, events, quotas, changes, labels, resources

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
    from . import schema
    schema.columns('node_agents', [('agent_version','INTEGER NOT NULL DEFAULT 1'),('last_management_at',"TEXT NOT NULL DEFAULT ''"),('core_version',"TEXT NOT NULL DEFAULT ''"),('core_sha256',"TEXT NOT NULL DEFAULT ''"),('probe_sha256',"TEXT NOT NULL DEFAULT ''"),('health_mode',"TEXT NOT NULL DEFAULT 'legacy'"),('management_enabled','INTEGER NOT NULL DEFAULT 1'),('core_channel',"TEXT NOT NULL DEFAULT 'legacy'"),('enrollment_reinstall','INTEGER NOT NULL DEFAULT 0'),('probe_failures','INTEGER NOT NULL DEFAULT 0')])
    c.commit()
    from . import monitor
    monitor.init()
    direct_nodes.init()
    telemetry.init()
    resources.init()
    events.init()
    quotas.init()
    changes.init()
    labels.init()
    regions.init()
    from . import operations, upgrades
    operations.init();upgrades.init()
    from . import ssh_control
    ssh_control.init()
    from . import recovery,task_center,schema
    recovery.init();task_center.init();schema.indexes()
    schema.columns('agent_jobs',[('error',"TEXT NOT NULL DEFAULT ''")])


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
    if not a:return labels.enrich(node)
    node.update(management='agent',agent_bound=bool(a['bound']),health_status=a['health_status'],
                last_report_at=a['last_report_at'],process_alive=bool(a['process_alive']),local_ws=bool(a['local_ws']),
                public_ws=bool(a['public_ws']),failure_count=a['failure_count'],repair_count=a['repair_count'],
                probe_status=a['probe_status'],probe_at=a['probe_at'],probe_error=a['probe_error'],latency_ms=a['latency_ms'],
                desired_port=json.loads(a['desired_json'])['node_port'],desired_revision=a['desired_revision'],applied_revision=a['applied_revision'])
    job=db.get_conn().execute("SELECT status FROM agent_jobs WHERE node_id=? AND revision=?",(node['id'],a['desired_revision'])).fetchone()
    node['job_status']=job['status'] if job else 'done'
    if db.get_node(node['id'])['status']=='uninstalling':node['status']='uninstalling';node['deleting']=True
    if a['bound'] and a.get('health_mode','legacy')!='none' and age(a['last_report_at'])>180:
        from . import monitor
        if a.get('health_mode')=='monitor' and not monitor.collector_available():node['health_status']='collector_unavailable'
        else:node['health_status']='offline';node['status']='offline'
    node.update(health_mode=a.get('health_mode','legacy'),management_enabled=bool(a.get('management_enabled',1)),core_channel=a.get('core_channel','legacy'))
    if node['health_mode']=='none':node['health_status']='unmonitored'
    b=cloudflare.binding(node['id'])
    desired=json.loads(a['desired_json'])
    node['protocol']=desired.get('protocol','vless-ws-argo')
    mode='fixed' if (b and b['status']=='active') or (not node.get('share_link') and desired.get('tunnel_mode')=='fixed') else 'temp'
    node.update(agent_version=a.get('agent_version',1),tunnel_mode=mode,fixed_hostname=b['hostname'] if b else desired.get('fixed_hostname',''),fixed_status=b['status'] if b else '')
    from . import operations
    node['operations']=operations.control(node['id'])
    from . import ssh_control
    node['management_transport']='ssh' if ssh_control.controls(node['id']) else 'agent'
    node.update(changes.public(node['id']))
    node.update(labels.enrich(node))
    node.pop('install_cmd',None);node.pop('uninstall_cmd',None)
    return node


class AgentEditBody(deploy.NodeEditBody):
    node_port: int=Field(ge=1,le=65535)
    @field_validator('client_port')
    @classmethod
    def port_valid(cls,v):
        if not 1<=v<=65535:raise ValueError('客户端端口应为 1–65535')
        return v

class CreateBody(AgentEditBody):
    core_channel: Literal['minimal','original']='minimal'
    install_monitor: bool=False
    management_enabled: bool=True
    protocol: Literal['vless-ws-argo','reality','hysteria2']='vless-ws-argo'
    server_name: str=Field(default='www.amd.com',pattern=r'^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$',max_length=253)
    @field_validator('server_name')
    @classmethod
    def normalize_sni(cls,v):return v.lower()
    tunnel_mode: Literal['temp','fixed']='temp'
    fixed_hostname: str=Field(default='',max_length=253)
    ws_path: str=Field(default='',max_length=128)
    uuid: str=Field(default='',max_length=36)
    @field_validator('ws_path')
    @classmethod
    def path_valid(cls,v):return deploy.DeployBody.path_valid(v)
    @field_validator('uuid')
    @classmethod
    def uuid_valid(cls,v):return deploy.DeployBody.uuid_valid(v)

class FixedBody(BaseModel):
    hostname: str=Field(default='',max_length=253)

class EnrollBody(BaseModel):
    enrollment_token: str=Field(min_length=20,max_length=128)
    hostname: str=Field(default='',max_length=253)
    os: str=Field(default='',max_length=100)
    arch: str=Field(default='',max_length=32)
    ip: str=Field(default='',max_length=64)

class Metadata(BaseModel):
    domain: str=Field(pattern=r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$',max_length=253)
    type: str=Field(default='temp',pattern=r'^(temp|fixed|direct)$')
    protocol: Literal['vless-ws-argo','reality','hysteria2']='vless-ws-argo'
    public_key: str=Field(default='',pattern=r'^(?:[A-Za-z0-9_-]{43})?$')
    short_id: str=Field(default='',pattern=r'^(?:[a-fA-F0-9]{16})?$')
    uuid: str=Field(max_length=36)
    path: str=Field(min_length=1,max_length=512)
    local_port: int=Field(ge=1,le=65535)
    @field_validator('uuid')
    @classmethod
    def uuid_valid(cls,v):return deploy.DeployBody.uuid_valid(v)

class Report(BaseModel):
    core_version: str=Field(default='',max_length=64)
    core_sha256: str=Field(default='',pattern=r'^(?:[a-f0-9]{64})?$')
    probe_sha256: str=Field(default='',pattern=r'^(?:[a-f0-9]{64})?$')
    resources: ResourceSample | None=None
    traffic: telemetry.Traffic | None=None
    agent_version: int=Field(default=1,ge=1,le=100)
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
    a=row(node_id)
    if a:return a
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


def enrollment(node_id,reinstall=False):
    ensure_node(node_id)
    token=secrets.token_urlsafe(32)
    c=db.get_conn();c.execute('UPDATE node_agents SET enrollment_hash=?,enrollment_until=?,enrollment_reinstall=? WHERE node_id=?',(digest(token),int(time.time())+86400,int(reinstall),node_id));c.commit()
    from . import controlplane
    command=controlplane.command('/agent/bootstrap/'+token+'.sh',PUBLIC_URL(),controlplane.for_node(node_id))
    return {'id':node_id,'command':command,'expires_in':86400,'agent_bound':bool(row(node_id)['bound']),'reinstall':reinstall}


def guard_reinstall(node_id,allowed_recovery=None):
    from . import ssh_control
    a=ensure_node(node_id)
    from . import recovery
    recovery.guard_node(node_id,allowed_recovery)
    if db.get_node(node_id)['status']=='uninstalling':raise HTTPException(409,'节点正在卸载')
    changes.guard(node_id);ssh_control.guard_enqueue(node_id)
    if db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone():
        raise HTTPException(409,'节点有未完成的任务，请先完成或处理该任务后再生成重装命令')
    return a


def enqueue(node_id,operation,config,allowed_recovery=None):
    from . import ssh_control
    from . import recovery
    recovery.guard_node(node_id,allowed_recovery)
    ssh_control.guard_enqueue(node_id)
    a=row(node_id);revision=a['desired_revision']+1;c=db.get_conn()
    c.execute('INSERT INTO agent_jobs(node_id,revision,operation,config_json,created_at) VALUES (?,?,?,?,?)',(node_id,revision,operation,json.dumps(config),db.utcnow()))
    # Release pins belong to this job, never to future edits/upgrades.
    desired={k:v for k,v in config.items() if k not in ('_upgrade_hashes','_release_sha256')}
    c.execute('UPDATE node_agents SET desired_revision=?,desired_json=?,last_job_error=\'\' WHERE node_id=?',(revision,json.dumps(desired),node_id))
    events.add(node_id,'job',f'已排队任务 {operation}，配置版本 {revision}')
    c.commit()
    db.update_node(node_id,status='installing' if a['bound'] else 'pending',last_error='')
    db.add_log(node_id,'已排队配置任务，等待节点下一次连接面板')
    return revision


def edit(node_id,body,allowed_recovery=None):
    from . import recovery
    recovery.guard_node(node_id,allowed_recovery)
    if db.get_node(node_id) and db.get_node(node_id)['status']=='uninstalling':raise HTTPException(409,'节点正在等待卸载')
    a=ensure_node(node_id);n=db.get_node(node_id)
    changes.guard(node_id)
    protocol=json.loads(a['desired_json']).get('protocol','vless-ws-argo')
    if protocol=='vless-ws-argo':
        if body.node_port<1024:raise HTTPException(400,'Argo 本地端口应为 1024–65535')
        try:deploy.DeployBody.port_valid(body.client_port)
        except ValueError as exc:raise HTTPException(400,str(exc))
    elif not body.client_address:raise HTTPException(400,'请输入公网连接地址')
    running=db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone()
    desired=json.loads(a['desired_json'])
    current_port=db.get_conn().execute('SELECT client_port FROM deployments WHERE node_id=?',(node_id,)).fetchone()['client_port']
    connection_changed=(body.node_port!=desired['node_port'] or body.client_address!=n['client_address'] or body.client_port!=current_port)
    if body.node_port!=desired['node_port'] and cloudflare.binding(node_id):
        raise HTTPException(409,'固定隧道请保持监听端口；优选地址与客户端端口可正常修改')
    if connection_changed and n['share_link']:
        if running:raise HTTPException(409,'已有任务等待执行，请完成后再修改连接配置')
        changes.begin(node_id)
    if body.node_port!=desired['node_port']:
        if cloudflare.binding(node_id):raise HTTPException(409,'固定隧道试运行期间请保持监听端口；优选地址与客户端端口可正常修改')
        desired['node_port']=body.node_port
        if not a['bound'] and not n['share_link'] and a['applied_revision']==0:
            c=db.get_conn();c.execute('UPDATE node_agents SET desired_json=?,actual_port=? WHERE node_id=?',(json.dumps(desired),body.node_port,node_id));c.execute("UPDATE agent_jobs SET config_json=? WHERE node_id=? AND status='queued'",(json.dumps(desired),node_id));c.commit()
            db.update_node(node_id,node_port=body.node_port)
        else:
            if running:raise HTTPException(409,'请先完成当前安装或配置任务，再修改监听端口')
            desired.update(client_address=body.client_address,client_port=body.client_port,remark=body.remark)
            enqueue(node_id,'update' if n['share_link'] else 'install',desired)
            if protocol in direct_nodes.KINDS:
                db.add_log(node_id,'直连端口变更已排队；保留当前链接，执行成功后同步新映射端口')
                changes.track(node_id)
                return enrich(deploy.enrich(db.get_node(node_id)))
    # Rebuild the client link immediately using the actual, currently applied metadata.
    link=n['share_link']
    if link:
        d=deploy.enrich(n)
        path=d['ws_path'] or parse_qs(urlparse(link).query).get('path',[''])[0]
        if protocol in direct_nodes.KINDS:link=direct_nodes.make_link(direct_nodes.state(node_id),body)
        else:link=deploy.make_link({'domain':n['sni'],'uuid':n['uuid'],'path':path},body)
    db.update_node(node_id,remark=body.remark,client_address=body.client_address,share_link=link)
    c=db.get_conn();c.execute('UPDATE deployments SET client_port=? WHERE node_id=?',(body.client_port,node_id))
    current_desired=json.loads(row(node_id)['desired_json'])
    current_desired.update(remark=body.remark,client_address=body.client_address,client_port=body.client_port)
    c.execute('UPDATE node_agents SET desired_json=? WHERE node_id=?',(json.dumps(current_desired),node_id))
    if not a['bound'] and not n['share_link'] and a['applied_revision']==0:
        c.execute("UPDATE agent_jobs SET config_json=? WHERE node_id=? AND status='queued'",(json.dumps(current_desired),node_id))
    if link!=n['share_link']:c.execute("UPDATE node_agents SET probe_status='unknown',probe_at='',probe_error='',latency_ms=NULL,probe_failures=0 WHERE node_id=?",(node_id,))
    c.commit()
    changes.track(node_id)
    if a['bound'] and (link!=n['share_link'] or changes.needs_confirmation(node_id,link)):schedule_probe(node_id)
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
    from . import monitor
    external_health=a.get('health_mode')=='monitor' and not getattr(report,'monitor_observation',False)
    if external_health:
        report.state=a['health_status'] if a['health_status'] in ('online','degraded','waiting') else 'waiting'
        report.process_alive=bool(a['process_alive']);report.local_ws=bool(a['local_ws']);report.public_ws=bool(a['public_ws'])
    metadata=report.metadata
    b=cloudflare.binding(node_id)
    protocol=json.loads(a['desired_json']).get('protocol','vless-ws-argo')
    direct=protocol in direct_nodes.KINDS
    if metadata and direct:
        if metadata.protocol!=protocol or metadata.type!='direct':raise HTTPException(409,'节点协议与绑定记录不一致')
        if protocol=='reality' and (not metadata.public_key or not metadata.short_id):raise HTTPException(409,'Reality 公钥不完整')
    if metadata and not direct:
        if metadata.local_port<1024:raise HTTPException(409,'Argo 上报端口无效')
        if metadata.type=='fixed':
            if not b or metadata.domain!=b['hostname']:raise HTTPException(409,'固定域名与当前节点绑定不一致')
        elif not metadata.domain.endswith('.trycloudflare.com') or (b and b['status']=='active'):
            raise HTTPException(409,'临时域名与当前节点隧道模式不一致')
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
    if job and report.job_ok and json.loads(job['config_json']).get('tunnel_mode')=='fixed' and (not metadata or metadata.type!='fixed'):
        raise HTTPException(409,'固定隧道任务尚未返回固定域名')
    c=db.get_conn();old_domain=node['sni'];changed=False
    try:
        c.execute('UPDATE node_agents SET agent_version=max(agent_version,?),last_management_at=? WHERE node_id=?',(report.agent_version,db.utcnow(),node_id))
        if report.agent_version>=8:
            c.execute('UPDATE node_agents SET core_version=?,core_sha256=?,probe_sha256=? WHERE node_id=?',(report.core_version,report.core_sha256,report.probe_sha256,node_id))
        if job and report.job_ok and metadata.type=='fixed':cloudflare.commit(node_id,metadata.local_port)
        c.execute('UPDATE node_agents SET last_report_at=?,health_status=?,process_alive=?,local_ws=?,public_ws=?,failure_count=?,last_repair=?,repair_count=? WHERE node_id=?',
                  (a['last_report_at'] if external_health else db.utcnow(),report.state,int(report.process_alive),int(report.local_ws),int(report.public_ws),a['failure_count'] if external_health else report.failure_count,report.last_repair,report.repair_count,node_id))
        if job:
            state='done' if report.job_ok else 'failed'
            c.execute('UPDATE agent_jobs SET status=?,finished_at=?,error=? WHERE node_id=? AND revision=?',(state,db.utcnow(),'' if report.job_ok else (report.error or '节点任务执行失败'),node_id,report.job_revision))
            if report.job_ok:
                c.execute('UPDATE node_agents SET applied_revision=?,actual_port=?,last_job_error=\'\' WHERE node_id=?',(report.job_revision,metadata.local_port,node_id))
            else:
                c.execute('UPDATE node_agents SET last_job_error=? WHERE node_id=?',(report.error or '节点任务执行失败',node_id))
        if metadata:
            values=json.loads(job['config_json']) if job and report.job_ok else {}
            restored=changes.restored_parameters(node_id,report.job_revision) if job and report.job_ok else {}
            values.update(restored)
            body=AgentEditBody(remark=values.get('remark',node['remark']),node_port=metadata.local_port,client_address=values.get('client_address',node['client_address']),client_port=values.get('client_port',db.get_conn().execute('SELECT client_port FROM deployments WHERE node_id=?',(node_id,)).fetchone()['client_port']))
            if (job and report.job_ok and ('client_address' in values or direct)) or restored:
                c.execute('UPDATE nodes SET remark=?,client_address=? WHERE id=?',(body.remark,body.client_address,node_id))
                c.execute('UPDATE deployments SET client_port=? WHERE node_id=?',(body.client_port,node_id))
            if direct:
                link=direct_nodes.make_link(metadata.model_dump(),body)
                c.execute('INSERT OR REPLACE INTO direct_node_state VALUES (?,?)',(node_id,json.dumps(metadata.model_dump())))
            else:link=deploy.make_link(metadata.model_dump(),body)
            changed=link!=node['share_link']
            c.execute('UPDATE nodes SET share_link=?,uuid=?,sni=?,node_port=? WHERE id=?',(link,metadata.uuid,metadata.domain,metadata.local_port,node_id))
            c.execute('UPDATE deployments SET ws_path=? WHERE node_id=?',(metadata.path,node_id))
            if changed:c.execute("UPDATE node_agents SET probe_status='unknown',probe_at='',probe_error='',latency_ms=NULL,probe_failures=0 WHERE node_id=?",(node_id,))
        if not direct and old_domain and metadata and metadata.type!='fixed' and old_domain!=metadata.domain:
            events.add(node_id,'tunnel','已同步临时隧道新域名，等待面板转发验证')
        events.health(node_id,report.state,report.process_alive,report.local_ws,report.public_ws,direct)
        if report.repair_count>a['repair_count']:
            events.add(node_id,'repair',f'自动修复次数增至 {report.repair_count}，当前上报状态 {report.state}','warning')
        if job:
            events.add(node_id,'job',f'配置版本 {report.job_revision} '+('执行成功，等待转发验证' if report.job_ok else '执行失败，保留原监听配置'),'info' if report.job_ok else 'warning')
        telemetry.record(node_id,report.traffic)
        resources.record(node_id,report.resources)
        c.commit()
    except Exception:c.rollback();raise
    latest=row(node_id)
    pending=c.execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone()
    error=latest['last_job_error']
    status='installing' if pending else ('error' if error else ('online' if report.local_ws and (direct or report.public_ws) else 'degraded'))
    if not metadata and not node['share_link'] and not pending:status='error'
    db.update_node(node_id,status=status,last_seen=db.utcnow(),last_error=error or report.error)
    if changed:db.add_log(node_id,'已同步节点最新域名和 VLESS 链接'+('（临时隧道域名已变化）' if old_domain and old_domain!=metadata.domain else ''))
    if report.repair_count>a['repair_count']:
        db.add_log(node_id,f'节点已尝试第 {report.repair_count} 次自动修复，当前状态：{report.state}')
    if job:
        db.add_log(node_id,f'配置版本 {report.job_revision} '+('执行成功' if report.job_ok else '执行失败：'+report.error))
        for line in report.logs:db.add_log(node_id,line)
    verify_restored=changes.job_result(node_id,report.job_revision,report.job_ok) if job else False
    if changed:changes.retarget(node_id)
    verify_core=monitor.core_job_result(node_id,job,report.job_ok) if job else False
    if verify_core or verify_restored or (changed and metadata and report.state!='installing'):schedule_probe(node_id)
    return {'ok':True,'applied_revision':latest['applied_revision']}


def job_script(a,job):
    config=json.loads(job['config_json']);n=deploy.enrich(db.get_node(a['node_id']))
    if job['operation']=='monitor_install':raise HTTPException(410,'独立资源探针已移除')
    if job['operation'] in ('core','core_rollback'):
        from . import monitor
        return monitor.job_script(a,job)
    if job['operation']=='uninstall':
        if config.get('protocol') in direct_nodes.KINDS:return direct_nodes.uninstall(a['node_id'],config,job['revision'])
        return (ROOT/'node-uninstall.sh').read_text().replace('__NODE_ID__',str(a['node_id'])).replace('__UUID__',shlex.quote(config['uuid'])).replace('__REVISION__',str(job['revision']))
    if job['operation'] in ('legacy_restore','monitor_remove'):
        from . import fixed_jobs
        script=fixed_jobs.upgrade(a['node_id'],config['node_port'],config.get('protocol','vless-ws-argo'))
        phase='restore' if job['operation']=='legacy_restore' else 'remove'
        setup='curl --config "$DIR/curl.conf" -fsS "$PANEL/agent/assets/restore-heartbeat.sh" -o "$TMP/restore.sh"\nsh -n "$TMP/restore.sh"\nsh "$TMP/restore.sh" '+phase+'\n'
        return script.replace("printf 'SB_WEB_RESULT='",setup+"printf 'SB_WEB_RESULT='",1)
    if job['operation']=='upgrade':
        from . import fixed_jobs
        return fixed_jobs.upgrade(a['node_id'],config['node_port'],config.get('protocol','vless-ws-argo'),config.get('_upgrade_hashes'))
    if config.get('protocol') in direct_nodes.KINDS:return direct_nodes.driver(a['node_id'],config,job['operation'])
    from . import fixed_jobs
    if job['operation']=='fixed' or config.get('tunnel_mode')=='fixed':
        if a.get('agent_version',1)<2:raise HTTPException(409,'请等待监控脚本升级完成')
        cloudflare.provision(a['node_id'],config.get('fixed_hostname',''),config['node_port'])
        if job['operation']=='install' or not n.get('share_link'):return fixed_jobs.install(a['node_id'],config,n['remark'])
        return fixed_jobs.migrate(a['node_id'],config)
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
    from . import recovery
    recovery.guard_node(node['id'])
    a=row(node['id'])
    if not a or not a['bound']:
        if node['share_link']:raise HTTPException(409,'请先接入监控，执行接入命令后才能从面板卸载远端节点')
        cloudflare.cleanup(node['id'])
        db.delete_node(node['id']);return {'ok':True,'deleted':True}
    if node['status']=='uninstalling':return {'ok':True,'pending':True}
    c=db.get_conn()
    c.execute("UPDATE agent_jobs SET status='failed',finished_at=? WHERE node_id=? AND status IN ('queued','running')",(db.utcnow(),node['id']));c.commit()
    config=json.loads(a['desired_json']);config['node_port']=a['actual_port']
    enqueue(node['id'],'uninstall',config)
    db.update_node(node['id'],status='uninstalling')
    db.add_log(node['id'],'已排队远端卸载，收到清理成功确认后自动删除记录')
    return {'ok':True,'pending':True,'manual_command':None if a['management_enabled'] else f"sh /usr/local/lib/argo-deploy/agent.sh /usr/local/etc/argo-deploy/node-{node['id']}"}


def force_delete(node):
    """Remove only panel state; never dispatch remote or cloud cleanup."""
    db.delete_node(node['id'])
    return {'ok':True,'deleted':True,'forced':True,'remote_cleanup_confirmed':False}


def deletion_blocked(node):
    a=row(node['id'])
    if not a:return node['status']=='installing' and bool(db.get_conn().execute('SELECT 1 FROM deployments WHERE node_id=?',(node['id'],)).fetchone())
    return age(a['last_report_at'])<180 and bool(db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status='running'",(node['id'],)).fetchone())


def repair_probe_state(a):
    return {'probe_failed':a['probe_status']=='failed' and a.get('probe_failures',0)>=2 and age(a['probe_at'])<600,
            'probe_at':a['probe_at'],'probe_failures':a.get('probe_failures',0)}


def schedule_probe(node_id):
    with PROBE_LOCK:
        if node_id in PROBING or len(PROBING)>=2:return False
        n=db.get_node(node_id)
        if not n or not n['share_link'] or n['status']=='uninstalling':return False
        a=row(node_id)
        if not a:return False
        revision=a['applied_revision']
        PROBING.add(node_id)
    c=db.get_conn();c.execute("UPDATE node_agents SET probe_status='pending' WHERE node_id=?",(node_id,));c.commit()
    def run():
        again=False
        try:
            from .node_probe import probe_link
            from .runtime_budget import Deferred
            for attempt in range(2):
                try:
                    result=probe_link(n['share_link']);state='ok';error='';latency=result['latency_ms']
                except Deferred:
                    db.get_conn().execute('UPDATE node_agents SET probe_status=? WHERE node_id=? AND applied_revision=?',(a['probe_status'] if a['probe_status']!='pending' else 'unknown',node_id,revision));db.get_conn().commit();return
                except Exception as e:state='failed';error=(type(e).__name__+': '+str(e))[:300];latency=None
                if state=='ok' or attempt==1:break
                if STOP.wait(2):return
            with LOCK:
                conn=db.get_conn()
                applied=conn.execute('UPDATE node_agents SET probe_status=?,probe_at=?,probe_error=?,latency_ms=?,probe_failures=CASE WHEN ? THEN 0 ELSE min(probe_failures+1,100) END WHERE node_id=? AND applied_revision=? AND EXISTS (SELECT 1 FROM nodes WHERE id=? AND share_link=? AND status!=\'uninstalling\')',(state,db.utcnow(),error,latency,state=='ok',node_id,revision,node_id,n['share_link'])).rowcount
                if applied:
                    events.observe(node_id,'probe',state,'实际协议转发验证通过' if state=='ok' else '实际协议转发验证失败：检查连接地址、端口映射及隧道入口','info' if state=='ok' else 'warning')
                    conn.commit()
                    from . import subscription_health
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='subscription_health_policy'").fetchone():
                        subscription_health.observe(node_id,state,row(node_id)['probe_at'],revision,n['share_link'])
                    again=changes.probe_result(node_id,n['share_link'],state=='ok')
                    from . import monitor
                    monitor.core_probe_result(node_id,revision,state=='ok')
                else:
                    conn.commit()
                    change=changes.row(node_id)
                    again=bool(change and change['state'] in ('verifying','verifying_rollback'))
        finally:
            with PROBE_LOCK:PROBING.discard(node_id)
            if again:schedule_probe(node_id)
            if getattr(db._local,'conn',None):
                db._local.conn.close();db._local.conn=None
    threading.Thread(target=run,daemon=True).start();return True


def probe_cycle():
    # Installation verification gets first use of the bounded probe slots.
    from . import recovery
    recovery.tick()
    for a in db.get_conn().execute('SELECT * FROM node_agents WHERE bound=1').fetchall():
        if age(a['probe_at'])>=300 or a['probe_status']=='pending':schedule_probe(a['node_id'])


def start_loop():
    global LOOP
    if LOOP and LOOP.is_alive():return
    STOP.clear()
    def loop():
        while not STOP.wait(30):
            from . import panel_health
            try:
                probe_cycle();panel_health.record('恢复调度',True)
            except Exception:panel_health.record('恢复调度',False)
            from . import sources, upgrades, panel_health, operations, ssh_control, recovery, subscription_health
            for name,fn in [('地区解析',regions.refresh_pending),('外部订阅调度',sources.tick),
                            ('事件整理',events.tick),('维护调度',operations.tick),('流量额度',quotas.tick),('升级调度',upgrades.tick),('SSH 调度',ssh_control.tick),('订阅隔离',subscription_health.tick),('通知调度',telemetry.tick)]:
                try:fn();panel_health.record(name,True)
                except Exception:panel_health.record(name,False)
            try:panel_health.tick()
            except Exception:panel_health.record('健康检查',False)
    LOOP=threading.Thread(target=loop,daemon=True);LOOP.start()


def create_node(body):
    from . import monitor
    if body.protocol=='vless-ws-argo':
        if body.node_port<1024:raise HTTPException(400,'Argo 本地端口应为 1024–65535')
        try:deploy.DeployBody.port_valid(body.client_port)
        except ValueError as exc:raise HTTPException(400,str(exc))
    elif not body.client_address:raise HTTPException(400,'直连节点需要公网连接地址或 NAT 出口地址')
    fixed_host=''
    if body.protocol=='vless-ws-argo' and body.tunnel_mode=='fixed':
        try:
            cfg=cloudflare.settings()
            if body.fixed_hostname:fixed_host=cloudflare.valid_hostname(body.fixed_hostname)
        except cloudflare.CFError as exc:raise HTTPException(400,str(exc))
    if body.install_monitor:raise HTTPException(410,'独立探针已移除，节点脚本内置心跳与管理')
    n=db.create_node(body.remark,body.node_port,'',body.client_address,secrets.token_urlsafe(24));config={'node_port':body.node_port,'ws_path':body.ws_path or '/'+secrets.token_hex(12),'uuid':body.uuid or str(uuid4()),'core_channel':body.core_channel}
    if body.protocol in direct_nodes.KINDS:config.update(protocol=body.protocol,server_name=body.server_name,ws_path='/')
    if body.protocol=='vless-ws-argo' and body.tunnel_mode=='fixed':config.update(tunnel_mode='fixed',fixed_hostname=fixed_host or f"node-{n['id']}.{cfg['zone_name']}")
    c=db.get_conn();c.execute('INSERT INTO deployments VALUES (?,?,?,?,?,?)',(n['id'],'',22,body.client_port,config['ws_path'],''));c.commit()
    db.update_node(n['id'],uuid=config['uuid'])
    ensure_node(n['id']);c.execute('UPDATE node_agents SET desired_json=? WHERE node_id=?',(json.dumps(config),n['id']));c.execute('UPDATE agent_jobs SET config_json=? WHERE node_id=? AND revision=1',(json.dumps(config),n['id']));c.commit()
    c.execute('UPDATE node_agents SET health_mode=?,management_enabled=?,core_channel=? WHERE node_id=?',('legacy',int(body.management_enabled),body.core_channel,n['id']));c.commit()
    db.add_log(n['id'],'协议安装命令已生成，请在目标服务器以 root 执行')
    result=enrollment(n['id'])
    return result


def register(app,require_login,public_url):
    global PUBLIC_URL
    PUBLIC_URL=public_url
    telemetry.register(app,require_login)
    events.register(app,require_login)
    quotas.register(app,require_login)
    def owner(request):
        require_login(request)
        if request.headers.get('origin') and urlparse(request.headers['origin']).netloc!=request.headers.get('host'):
            raise HTTPException(403,'请求来源不匹配')
    regions.register(app,owner)
    labels.register(app,owner)
    resources.register(app,owner)
    from . import monitor
    monitor.register(app,owner,public_url)
    from . import operations, upgrades, panel_health
    operations.register(app,owner);upgrades.register(app,owner);panel_health.register(app,owner)
    from . import ssh_control
    ssh_control.register(app,owner)
    from . import servers,recovery,task_center
    servers.register(app,owner);recovery.register(app,owner);task_center.register(app,owner)
    @app.post('/api/agent-nodes')
    def create(body:CreateBody,request:Request):
        owner(request)
        with LOCK:return create_node(body)
    @app.post('/api/agent-nodes/{node_id}/fixed')
    def migrate_fixed(node_id:int,body:FixedBody,request:Request):
        owner(request)
        with LOCK:
            a=ensure_node(node_id);n=db.get_node(node_id)
            changes.guard(node_id)
            if json.loads(a['desired_json']).get('protocol','vless-ws-argo')!='vless-ws-argo':raise HTTPException(400,'仅 Argo 节点支持固定隧道')
            if not a['bound'] or not n['share_link']:raise HTTPException(409,'请先完成节点部署并接入监控')
            if n['status']=='uninstalling' or age(a['last_report_at'])>180:raise HTTPException(409,'节点正在卸载或未在线上报')
            if db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone():raise HTTPException(409,'请先完成当前任务')
            config=json.loads(a['desired_json']);config['node_port']=a['actual_port']
            try:
                b=cloudflare.provision(node_id,body.hostname,config['node_port'])
            except cloudflare.CFError as exc:raise HTTPException(502,str(exc))
            if a.get('agent_version',1)<2:enqueue(node_id,'upgrade',config)
            config.update(tunnel_mode='fixed',fixed_hostname=b['hostname'])
            revision=enqueue(node_id,'fixed',config)
            return {'ok':True,'revision':revision,'hostname':b['hostname']}
    @app.post('/api/agent-nodes/{node_id}/enrollment')
    def make_enrollment(node_id:int,request:Request):
        owner(request)
        with LOCK:
            recovery.guard_node(node_id)
            return enrollment(node_id)
    @app.post('/api/agent-nodes/{node_id}/reinstall')
    def reinstall_node(node_id:int,request:Request):
        owner(request)
        with LOCK:
            a=guard_reinstall(node_id)
            result=enrollment(node_id,reinstall=True)
            return result
    @app.patch('/api/agent-nodes/{node_id}')
    def edit_node(node_id:int,body:AgentEditBody,request:Request):
        owner(request)
        with LOCK:return edit(node_id,body)
    @app.post('/api/agent-nodes/{node_id}/upgrade')
    def upgrade_agent(node_id:int,request:Request):
        owner(request)
        with LOCK:
            a=ensure_node(node_id)
            changes.guard(node_id)
            if not a['bound'] or db.get_node(node_id)['status']=='uninstalling':raise HTTPException(409,'节点未绑定或正在卸载')
            if db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone():raise HTTPException(409,'已有任务等待执行')
            config=json.loads(a['desired_json']);config['node_port']=a['actual_port']
            return {'ok':True,'revision':enqueue(node_id,'upgrade',config)}
    @app.post('/api/agent-nodes/{node_id}/refresh')
    def refresh(node_id:int,request:Request):
        owner(request)
        with LOCK:
            a=ensure_node(node_id)
            changes.guard(node_id)
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
            baseline=changes.prepare_retry(node_id)
            changes.guard(node_id)
            config=changes.retry_config(node_id) or json.loads(a['desired_json']);n=db.get_node(node_id)
            changes.begin(node_id)
            if baseline:
                db.get_conn().execute('UPDATE node_changes SET baseline_ok=1 WHERE node_id=?',(node_id,));db.get_conn().commit()
            operation=('update' if config['node_port']!=a['actual_port'] else 'read') if n['share_link'] else 'install'
            revision=enqueue(node_id,operation,config)
            changes.track(node_id)
            return {'ok':True,'revision':revision,'agent_bound':bool(a['bound'])}
    @app.post('/api/agent-nodes/{node_id}/probe')
    def probe_now(node_id:int,request:Request):
        owner(request)
        if not row(node_id):raise HTTPException(400,'请先接入监控脚本')
        return {'scheduled':schedule_probe(node_id)}
    @app.get('/agent/bootstrap/{token}.sh')
    def bootstrap(token:str,control:str|None=None):
        a=db.get_conn().execute('SELECT * FROM node_agents WHERE enrollment_hash=? AND enrollment_until>?',(digest(token),int(time.time()))).fetchone()
        if not a:raise HTTPException(410,'安装命令已使用或已过期，请在面板重新生成')
        from .agent_scripts import bootstrap_script
        from . import controlplane
        options=dict(a)
        if ssh_control.controls(a['node_id']):options.update(management_enabled=0,ssh_transport=True)
        return PlainTextResponse(bootstrap_script(controlplane.selected(public_url(),controlplane.for_node(a['node_id']),control),token,a['node_id'],options),media_type='text/x-shellscript')
    @app.get('/agent/assets/{name}')
    def asset(name:str):
        files={'restore-heartbeat.sh':'node-restore-heartbeat.sh','agent.sh':'node-agent.sh','repair.sh':'node-repair.sh','fixed.sh':'node-fixed.sh','traffic.sh':'node-traffic.sh','resources.sh':'node-resources.sh','diagnose.sh':'node-diagnose.sh','core.sh':'node-core.sh'}
        if name=='library.sh':text=deploy.installer_text().rsplit('\nmain',1)[0]+'\n'
        elif name in files:text=(ROOT/files[name]).read_text()
        else:raise HTTPException(404,'资源不存在')
        return PlainTextResponse(text,media_type='text/x-shellscript')
    @app.post('/agent/enroll')
    def enroll(body:EnrollBody):
        with LOCK:
            a=db.get_conn().execute('SELECT * FROM node_agents WHERE enrollment_hash=? AND enrollment_until>?',(digest(body.enrollment_token),int(time.time()))).fetchone()
            if not a:raise HTTPException(410,'安装凭据已使用或已过期')
            if a['enrollment_reinstall']:
                recovery_id=recovery.enrollment_run(a['enrollment_hash'])
                guard_reinstall(a['node_id'],recovery_id)
                enqueue(a['node_id'],'install',json.loads(a['desired_json']),allowed_recovery=recovery_id)
                if not operations.maintenance(a['node_id']):operations.set_maintenance('node',str(a['node_id']),20,'执行重装恢复命令')
                events.add(a['node_id'],'job','已执行重装恢复命令，按面板保存配置重新安装协议')
            token=secrets.token_urlsafe(32);c=db.get_conn();c.execute("UPDATE node_agents SET token_hash=?,bound=1,enrollment_hash=NULL,enrollment_until=0,enrollment_reinstall=0,health_mode='legacy',health_status='waiting',last_report_at=? WHERE node_id=?",(digest(token),db.utcnow(),a['node_id']));c.commit()
            n=db.get_node(a['node_id']);fields={'hostname':body.hostname,'os_info':(body.os+' '+body.arch).strip()}
            try:fields['host_ip']=str(ipaddress.ip_address(body.ip))
            except ValueError:
                if not n['host_ip']:fields['host_ip']=body.hostname
            db.update_node(n['id'],**fields);db.add_log(n['id'],'节点已通过一次性安装凭据绑定，开始主动上报')
            return {'node_id':n['id'],'agent_token':token,'port':a['actual_port'],'applied_revision':a['applied_revision'],'protocol':json.loads(a['desired_json']).get('protocol','vless-ws-argo'),'core_channel':a['core_channel'],'management_enabled':bool(a['management_enabled']),'health_mode':'legacy'}
    @app.post('/agent/uninstalled/{revision}')
    def uninstalled(revision:int,x_agent_token:str|None=Header(default=None)):
        with LOCK:
            c=db.get_conn()
            c.execute('DELETE FROM uninstall_receipts WHERE expires<?',(int(time.time()),));c.commit()
            if x_agent_token and c.execute('SELECT 1 FROM uninstall_receipts WHERE token_hash=? AND revision=?',(digest(x_agent_token),revision)).fetchone():return {'ok':True}
            a=authorize(x_agent_token)
            job=db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND revision=? AND operation='uninstall' AND status='running'",(a['node_id'],revision)).fetchone()
            if not job:raise HTTPException(409,'无有效卸载任务')
            try:cloudflare.cleanup(a['node_id'])
            except cloudflare.CFError as exc:raise HTTPException(502,str(exc))
            c.execute('INSERT OR REPLACE INTO uninstall_receipts VALUES (?,?,?)',(a['token_hash'],revision,int(time.time())+86400))
            db.delete_node(a['node_id'])
            return {'ok':True}
    @app.get('/agent/fixed-config')
    def fixed_config(x_agent_token:str|None=Header(default=None)):
        a=authorize(x_agent_token)
        try:return cloudflare.credentials(a['node_id'])
        except cloudflare.CFError as exc:raise HTTPException(502,str(exc))
    @app.post('/agent/restore-heartbeat')
    def restore_heartbeat(request:Request,x_agent_token:str=Header(default='')):
        a=authorize(x_agent_token)
        with LOCK:
            db.get_conn().execute("UPDATE node_agents SET health_mode='legacy',management_enabled=1 WHERE node_id=?",(a['node_id'],));db.get_conn().commit()
        return {'ok':True}
    @app.get('/agent/work')
    def work(x_agent_token:str|None=Header(default=None),x_ssh_execution:str|None=Header(default=None)):
        a=authorize(x_agent_token)
        job=db.get_conn().execute("SELECT revision,operation,config_json FROM agent_jobs WHERE node_id=? AND status IN ('queued','running') AND revision>? ORDER BY revision LIMIT 1",(a['node_id'],a['applied_revision'])).fetchone()
        state=operations.control(a['node_id'])
        if ssh_control.controls(a['node_id']) and x_ssh_execution!='1':
            job=None;state['repair_allowed']=False
        health={**repair_probe_state(a),'failure_count':a['failure_count'],'health_status':a['health_status'],'process_alive':bool(a['process_alive']),'local_ws':bool(a['local_ws'])}
        if not state['repair_allowed']:health.update(probe_failed=False,failure_count=0)
        return {'job':dict(job) if job else None,'health_mode':a['health_mode'],'health':health,'control':state}
    @app.get('/agent/job/{revision}.sh')
    def get_job(revision:int,x_agent_token:str|None=Header(default=None),x_ssh_execution:str|None=Header(default=None)):
        with LOCK:
            a=authorize(x_agent_token)
            if ssh_control.controls(a['node_id']) and x_ssh_execution!='1':raise HTTPException(409,'该节点由 SSH 通道执行管理任务')
            job=db.get_conn().execute("SELECT * FROM agent_jobs WHERE node_id=? AND revision=? AND status IN ('queued','running')",(a['node_id'],revision)).fetchone()
            if not job:raise HTTPException(409,'任务已完成或已取消')
            c=db.get_conn();c.execute("UPDATE agent_jobs SET status='running' WHERE node_id=? AND revision=?",(a['node_id'],revision));c.commit()
            db.update_node(a['node_id'],status='uninstalling' if job['operation']=='uninstall' else 'installing')
            try:script=job_script(a,job)
            except cloudflare.CFError as exc:
                c.execute("UPDATE agent_jobs SET status='failed',finished_at=?,error=? WHERE node_id=? AND revision=?",(db.utcnow(),str(exc),a['node_id'],revision))
                c.execute('UPDATE node_agents SET last_job_error=? WHERE node_id=?',(str(exc),a['node_id']));c.commit()
                db.update_node(a['node_id'],status='error',last_error=str(exc));db.add_log(a['node_id'],'固定隧道准备失败：'+str(exc))
                raise HTTPException(502,str(exc))
            return PlainTextResponse(script,media_type='text/x-shellscript')
    @app.post('/agent/report')
    def report(body:Report,x_agent_token:str|None=Header(default=None)):
        with LOCK:return accept_report(authorize(x_agent_token),body)
