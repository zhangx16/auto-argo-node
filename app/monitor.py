"""Private Monitor adapter: host identity, bounded WS gateway and service health."""
import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import time
import urllib.request
from pathlib import Path
from uuid import uuid4
from typing import Literal
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator
from . import db, cloudflare
from .telemetry import Traffic

ROOT=Path(__file__).parent
RELEASES=Path(os.environ.get('ARGO_RELEASES','/app/releases'))
HUB=os.environ.get('MONITOR_HUB_URL','http://monitor-hub:28080').rstrip('/')
PUBLIC_URL=None
SESSIONS={}
COLLECTOR={"at":0.,"ok":True}

def collector_available():
    if time.monotonic()-COLLECTOR["at"]>25:
        try:hub("/nodes");ok=True
        except HTTPException:ok=False
        COLLECTOR.update(at=time.monotonic(),ok=ok)
    return COLLECTOR["ok"]


def init():
    c=db.get_conn();c.executescript('''
    CREATE TABLE IF NOT EXISTS monitor_hosts (
      id TEXT PRIMARY KEY,name TEXT NOT NULL,hub_id INTEGER NOT NULL UNIQUE,hub_token TEXT NOT NULL,
      token_hash TEXT NOT NULL UNIQUE,enrollment_hash TEXT UNIQUE,enrollment_until INTEGER NOT NULL DEFAULT 0,
      token_secret TEXT NOT NULL,last_seen INTEGER NOT NULL DEFAULT 0,health TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS core_rollouts(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,revision INTEGER NOT NULL,state TEXT NOT NULL,baseline_ok INTEGER NOT NULL,previous_channel TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS monitor_nodes (
      node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      host_id TEXT NOT NULL REFERENCES monitor_hosts(id) ON DELETE CASCADE);
    ''')
    from . import schema
    schema.columns('monitor_hosts',[('group_name', "TEXT NOT NULL DEFAULT ''"), ('sort_order', 'INTEGER NOT NULL DEFAULT 0')],version=1)
    c.commit()

def digest(s):return hashlib.sha256(s.encode()).hexdigest()

def hub(path,method='GET',body=None):
    token=os.environ.get('ARGO_MONITOR_SERVICE_TOKEN','')
    if len(token)<32:raise HTTPException(503,'Monitor 尚未配置')
    request=urllib.request.Request(HUB+'/api'+path,method=method,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json','X-Argo-Service':token,'Origin':PUBLIC_URL() if PUBLIC_URL else 'https://panel.example.com'})
    try:
        with urllib.request.urlopen(request,timeout=8) as response:
            raw=response.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:raise ValueError('oversize')
            return json.loads(raw) if raw else {}
    except Exception as e:
        raise HTTPException(503,'Monitor 采集服务暂不可用') from e

class HostTrafficRule(BaseModel):
    model_config=ConfigDict(extra='forbid')
    traffic_limit:int=Field(default=0,ge=0,le=2**60,strict=True)
    traffic_mode:Literal['sum','up','down','max']='sum'
    traffic_reset_day:int=Field(default=1,ge=1,le=31,strict=True)


def host_traffic(remote):
    if remote is None:return None
    rx=max(0,int(remote.get('month_rx',0)));tx=max(0,int(remote.get('month_tx',0)))
    mode=remote.get('traffic_mode','sum')
    if mode not in ('sum','up','down','max'):mode='sum'
    used={'sum':rx+tx,'up':tx,'down':rx,'max':max(rx,tx)}[mode]
    limit=max(0,int(remote.get('traffic_limit',0)))
    return {'limit_bytes':limit,'used_bytes':used,'rx':rx,'tx':tx,'count_mode':mode,
      'reset_day':remote.get('traffic_reset_day',1),'cycle_start':remote.get('month_start',''),
      'percent':round(used/limit*100,2) if limit else None,
      'remaining_bytes':max(0,limit-used) if limit else None,'exceeded':bool(limit and used>=limit),'timezone':'UTC'}


def set_host_traffic(host_id,body):
    h=get_host(host_id)
    hub(f"/nodes/{h['hub_id']}",'PUT',body.model_dump())
    return {'ok':True}


class HostGroup(BaseModel):
    model_config=ConfigDict(extra='forbid')
    group_name:str=Field(default='',max_length=40)
    @field_validator('group_name')
    @classmethod
    def clean_group(cls,value):
        from .labels import clean
        return clean(value,40)

class HostOrder(BaseModel):
    model_config=ConfigDict(extra='forbid')
    host_ids:list[str]=Field(min_length=1,max_length=2000)
    @field_validator('host_ids')
    @classmethod
    def unique_hosts(cls,values):
        if len(set(values))!=len(values) or any(not v or len(v)>64 for v in values):raise ValueError('服务器列表包含重复或无效编号')
        return values


def ordered_host_ids():
    return [r['id'] for r in db.get_conn().execute('SELECT id FROM monitor_hosts ORDER BY sort_order,name,id')]


def reorder_hosts(body):
    c=db.get_conn();order=ordered_host_ids();selected=set(body.host_ids)
    if not selected.issubset(set(order)):raise HTTPException(409,'服务器列表已变化，请刷新后重新排序')
    replacement=iter(body.host_ids)
    order=[next(replacement) if host_id in selected else host_id for host_id in order]
    c.executemany('UPDATE monitor_hosts SET sort_order=? WHERE id=?',enumerate(order));c.commit()
    return {'host_ids':order}


def set_host_group(host_id,body):
    get_host(host_id)
    c=db.get_conn();c.execute('UPDATE monitor_hosts SET group_name=? WHERE id=?',(body.group_name,host_id))
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='ssh_servers'").fetchone():c.execute('UPDATE ssh_servers SET group_name=? WHERE monitor_host_id=?',(body.group_name,host_id))
    c.commit()
    return {'group_name':body.group_name}


def require_available():hub('/nodes')

def get_host(host_id):
    row=db.get_conn().execute('SELECT * FROM monitor_hosts WHERE id=?',(host_id,)).fetchone()
    if not row:raise HTTPException(404,'监控主机不存在')
    return dict(row)

def enrollment_for_node(node_id,host_id=None):
    node=db.get_node(node_id)
    if not node:raise HTTPException(404,'节点不存在')
    c=db.get_conn()
    old=c.execute('SELECT host_id FROM monitor_nodes WHERE node_id=?',(node_id,)).fetchone()
    if old and host_id and old['host_id']!=host_id:raise HTTPException(409,'节点已关联其他主机，请先解除关联')
    if host_id or old:
        h=get_host(host_id or old['host_id'])
    else:
        remote=hub('/nodes','POST',{'name':node['remark'][:64] or f"Host {node_id}",'public':False,'notify':False})
        try:remote_token=hub(f"/nodes/{remote['id']}/token")['token']
        except Exception:
            hub(f"/nodes/{remote['id']}",'DELETE');raise
        host_id=str(uuid4());token=secrets.token_urlsafe(32)
        c.execute('INSERT INTO monitor_hosts(id,name,hub_id,hub_token,token_hash,token_secret) VALUES (?,?,?,?,?,?)',(host_id,node['remark'],remote['id'],cloudflare.encrypt(remote_token),digest(token),cloudflare.encrypt(token)))
        c.execute('UPDATE monitor_hosts SET sort_order=(SELECT coalesce(max(sort_order),-1)+1 FROM monitor_hosts WHERE id!=?) WHERE id=?',(host_id,host_id));c.commit()
        h=get_host(host_id)
    c.execute('INSERT OR IGNORE INTO monitor_nodes VALUES (?,?)',(node_id,h['id']))
    token=secrets.token_urlsafe(32)
    c.execute('UPDATE monitor_hosts SET enrollment_hash=?,enrollment_until=? WHERE id=?',(digest(token),int(time.time())+86400,h['id']));c.commit()
    from . import controlplane
    command=controlplane.command('/monitor/bootstrap/'+token+'.sh',PUBLIC_URL(),controlplane.for_host(h['id']))
    return {'host_id':h['id'],'command':command,'expires_in':86400}

class HostBinding(BaseModel):
    host_id:str|None=None
class CoreChoice(BaseModel):
    channel:str=Field(pattern='^(minimal|original|rollback)$')
class Service(BaseModel):
    model_config=ConfigDict(allow_inf_nan=False)
    node_id:int=Field(ge=1)
    revision:int=Field(ge=0)
    port:int=Field(ge=1,le=65535)
    protocol:str=Field(max_length=32)
    process_alive:bool=False
    local_listen:bool=False
    domain:str=Field(default='',max_length=253,pattern=r'^(?:|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})$')
    path:str=Field(default='',max_length=512)
    traffic:Traffic|None=None
    core:dict|None=None
    published_at:int=Field(default=0,ge=0)
class ServiceReport(BaseModel):
    probe_rss:int=Field(default=0,ge=0,le=2**60)
    version:int=Field(ge=1,le=1)
    sampled_at:int=Field(ge=0)
    singbox_rss:int=Field(ge=0,le=2**60)
    cloudflared_rss:int=Field(ge=0,le=2**60)
    singbox_processes:int=Field(ge=0,le=100000)
    cloudflared_processes:int=Field(ge=0,le=100000)
    services:list[Service]=Field(max_length=128)

def observe(host_id,payload):
    from . import agent,events,telemetry
    report=ServiceReport.model_validate(payload)
    now=int(time.time());c=db.get_conn();h=get_host(host_id)
    with agent.LOCK:
        ids={r['node_id'] for r in c.execute('SELECT node_id FROM monitor_nodes WHERE host_id=?',(host_id,))}
        safe=report.model_dump();safe['received_at']=now;safe['services']=[]
        seen=set()
        for service in report.services:
            if service.node_id in seen:continue
            seen.add(service.node_id)
            if service.node_id not in ids:continue
            a=agent.row(service.node_id);node=db.get_node(service.node_id)
            if not a or not node or service.revision!=a['applied_revision'] or service.port!=a['actual_port']:continue
            cfg=json.loads(a['desired_json'])
            if service.protocol!=cfg.get('protocol','vless-ws-argo'):continue
            safe['services'].append({'node_id':service.node_id,'process_alive':service.process_alive,'local_listen':service.local_listen,'core':service.core})
            if a['health_mode']!='monitor':continue # staged until explicit activation
            alive=service.process_alive and service.local_listen
            status='online' if alive else 'degraded'
            failures=0 if alive else min(100000,a['failure_count']+1)
            c.execute('UPDATE node_agents SET last_report_at=?,health_status=?,process_alive=?,local_ws=?,failure_count=? WHERE node_id=?',(db.utcnow(),status,int(service.process_alive),int(service.local_listen),failures,service.node_id))
            if node['status'] not in ('installing','uninstalling'):c.execute('UPDATE nodes SET status=?,last_seen=? WHERE id=?',(status,db.utcnow(),service.node_id))
            events.health(service.node_id,status,service.process_alive,service.local_listen,False,True)
            # Metadata contains no credential; reconstruct identity from the authenticated binding.
            if service.protocol=='vless-ws-argo' and service.domain and service.path and service.published_at>now-180:
                binding=cloudflare.binding(service.node_id)
                permitted=(binding and binding['status']=='active' and service.domain==binding['hostname']) or (not binding and service.domain.endswith('.trycloudflare.com'))
                if permitted and service.path==cfg.get('ws_path') and service.domain!=node['sni']:
                    from . import deploy,changes
                    body=agent.AgentEditBody(remark=node['remark'],node_port=service.port,client_address=node['client_address'],client_port=c.execute('SELECT client_port FROM deployments WHERE node_id=?',(service.node_id,)).fetchone()['client_port'])
                    meta={'domain':service.domain,'uuid':cfg['uuid'],'path':service.path,'local_port':service.port}
                    link=deploy.make_link(meta,body)
                    c.execute('UPDATE nodes SET share_link=?,sni=? WHERE id=?',(link,service.domain,service.node_id))
                    c.execute("UPDATE node_agents SET probe_status='unknown',probe_at='' WHERE node_id=?",(service.node_id,))
                    events.add(service.node_id,'tunnel','探针同步临时隧道域名，等待面板转发验证')
            if service.traffic and service.published_at>now-180:
                telemetry.record(service.node_id,service.traffic)
            if service.core and service.core.get('channel') in ('minimal','original','restored'):
                c.execute('UPDATE node_agents SET core_channel=? WHERE node_id=?',(service.core['channel'],service.node_id))
        c.execute('UPDATE monitor_hosts SET health=? WHERE id=?',(json.dumps(safe),host_id));c.commit()

def job_script(a,job):
    config=json.loads(job['config_json']);node_id=a['node_id']
    if job['operation']=='monitor_install':
        host=get_host(config['monitor_host_id'])
        script=install_script(host)
        script=script.replace('set -eu\n','set -eu\nexport ARGO_RELEASE_SHA256='+shlex.quote(config.get('_release_sha256',''))+'\n',1)
    else:
        action='_panel_rollback_core' if job['operation']=='core_rollback' else '_panel_update_selected_core'
        channel=config.get('core_channel','minimal')
        script=f'''#!/bin/sh
set -eu
export ARGO_PANEL=$(jq -r .panel /usr/local/etc/argo-deploy/node-{node_id}/settings.json)
export ARGO_CORE_CHANNEL={shlex.quote(channel)}
export ARGO_RELEASE_SHA256={shlex.quote(config.get("_release_sha256",""))}
curl -fsS "$ARGO_PANEL/agent/assets/core.sh" -o /usr/local/lib/argo-deploy/core.sh.new
bash -n /usr/local/lib/argo-deploy/core.sh.new
mv /usr/local/lib/argo-deploy/core.sh.new /usr/local/lib/argo-deploy/core.sh
bash -c 'source /usr/local/lib/argo-deploy/library.sh agent-library; source /usr/local/lib/argo-deploy/core.sh; _detect_init_system; _panel_update_selected_core() {{ if _singbox_core_is_locked; then _do_update_singbox "$SINGBOX_FIXED_VERSION"; else _do_update_singbox latest; fi; }}; _with_state_lock {action}'
'''
    meta='cat "$DIR/direct.json"' if config.get('protocol') in ('reality','hysteria2') else "jq -c --arg t argo-vless-ws-"+str(config['node_port'])+" '.[$t]' /usr/local/etc/sing-box/argo_metadata.json"
    return script+f'\nDIR=/usr/local/etc/argo-deploy/node-{node_id}\nprintf "SB_WEB_RESULT="\n'+meta+'\n'

def install_script(h,control=None):
    from . import controlplane
    panel=controlplane.selected(PUBLIC_URL(),controlplane.for_host(h['id']),control)
    constants='\n'.join(k+'='+shlex.quote(str(v)) for k,v in {'PANEL':panel,'HOST_ID':h['id'],'MONITOR_TOKEN':cloudflare.decrypt(h['token_secret'])}.items())
    return '#!/bin/sh\nset -eu\numask 077\n'+constants+'\n'+(ROOT/'node-monitor-install.sh').read_text()

def register(app,owner,public_url):
    global PUBLIC_URL
    PUBLIC_URL=public_url
    @app.get('/monitor',include_in_schema=False)
    @app.get('/monitor/',include_in_schema=False)
    def removed(request:Request):
        owner(request)
        return RedirectResponse('/static/index.html#traffic-module',status_code=303)
    @app.get('/agent/releases/{name}')
    def release(name:str):
        if name!='manifest.json' and not re.fullmatch(r'sing-box-(minimal|original)(?:-1\.13\.21)?-(amd64|arm64)',name):raise HTTPException(404)
        path=RELEASES/name
        if not path.is_file():raise HTTPException(503,'该架构发布文件尚未就绪')
        return FileResponse(path,headers={'Cache-Control':'no-store'})
    @app.post('/api/agent-nodes/{node_id}/core')
    def core(node_id:int,body:CoreChoice,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:
            a=agent.ensure_node(node_id)
            if db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(node_id,)).fetchone():raise HTTPException(409,'请先完成当前任务')
            same_host=db.get_conn().execute('SELECT host_id FROM monitor_nodes WHERE node_id=?',(node_id,)).fetchone()
            if same_host and db.get_conn().execute("SELECT 1 FROM agent_jobs j JOIN monitor_nodes m ON m.node_id=j.node_id WHERE m.host_id=? AND j.status IN ('queued','running')",(same_host['host_id'],)).fetchone():raise HTTPException(409,'同一主机已有配置任务，请完成后再切换核心')
            cfg=json.loads(a['desired_json']);cfg['node_port']=a['actual_port']
            if body.channel!='rollback':cfg['core_channel']=body.channel
            rev=agent.enqueue(node_id,'core_rollback' if body.channel=='rollback' else 'core',cfg)
            db.get_conn().execute('INSERT OR REPLACE INTO core_rollouts VALUES (?,?,?,?,?)',(node_id,rev,'rollback' if body.channel=='rollback' else 'applying',int(a['probe_status']=='ok' and agent.age(a['probe_at'])<600),a['core_channel']));db.get_conn().commit()
            return {'revision':rev,'manual_command':None if a['management_enabled'] else f'sh /usr/local/lib/argo-deploy/agent.sh /usr/local/etc/argo-deploy/node-{node_id}'}

def core_job_result(node_id,job,ok):
    if not job or job['operation'] not in ('core','core_rollback'):return False
    c=db.get_conn()
    if ok:
        channel=json.loads(job['config_json']).get('core_channel','original') if job['operation']=='core' else 'restored'
        c.execute('UPDATE node_agents SET core_channel=? WHERE node_id=?',(channel,node_id))
    c.execute('UPDATE core_rollouts SET state=? WHERE node_id=? AND revision=?',(('verifying_rollback' if job['operation']=='core_rollback' else 'verifying') if ok else 'failed',node_id,job['revision']));c.commit()
    return bool(ok)

def core_probe_result(node_id,revision,ok):
    from . import agent,events
    c=db.get_conn();r=c.execute('SELECT * FROM core_rollouts WHERE node_id=? AND revision=?',(node_id,revision)).fetchone()
    if not r or r['state'] not in ('verifying','verifying_rollback'):return
    if ok:state='restored' if r['state']=='verifying_rollback' else 'succeeded'
    elif r['state']=='verifying_rollback':state='rollback_failed'
    elif not r['baseline_ok']:state='unverified'
    else:
        a=agent.row(node_id);cfg=json.loads(a['desired_json']);cfg['node_port']=a['actual_port']
        rev=agent.enqueue(node_id,'core_rollback',cfg)
        c.execute('UPDATE core_rollouts SET revision=? WHERE node_id=?',(rev,node_id))
        events.add(node_id,'core','新核心转发验证失败，已排队恢复上一份核心','warning')
        state='rollback'
    labels={'restored':'已恢复上一核心并通过实际转发验证','succeeded':'新核心已通过实际转发验证','rollback_failed':'核心回滚后转发仍失败，需要人工检查','unverified':'新核心转发验证失败，缺少成功基线，未自动回滚'}
    if state in labels:events.add(node_id,'core',labels[state],'warning' if state in ('rollback_failed','unverified') else 'info')
    c.execute('UPDATE core_rollouts SET state=? WHERE node_id=?',(state,node_id));c.commit()
