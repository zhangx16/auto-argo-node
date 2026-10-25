"""Canonical server inventory over existing SSH IDs and explicit probe/node bindings."""
import json
import time
from fastapi import HTTPException,Request
from pydantic import BaseModel,Field
from . import db,controlplane,capabilities


def init():
    from . import schema
    schema.columns('ssh_servers',[
      ('control_url',"TEXT NOT NULL DEFAULT ''"),('fallback_urls',"TEXT NOT NULL DEFAULT '[]'"),
      ('capabilities',"TEXT NOT NULL DEFAULT '{}'"),('capabilities_at','REAL NOT NULL DEFAULT 0'),
      ('group_name',"TEXT NOT NULL DEFAULT ''"),('notes',"TEXT NOT NULL DEFAULT ''")],version=2)
    schema.apply('canonical-server-groups',1,lambda c:c.execute("UPDATE ssh_servers SET group_name=coalesce((SELECT group_name FROM monitor_hosts h WHERE h.id=ssh_servers.monitor_host_id),'') WHERE group_name=''"))


def describe(s):
    from . import ssh_control,agent,node_names
    result=ssh_control.public(s)
    result.update(control_url=s['control_url'],fallback_urls=json.loads(s['fallback_urls']),group_name=s['group_name'],notes=s['notes'],capabilities=capabilities.cached(s))
    monitor=None;result['monitor']=None
    rows=db.get_conn().execute('''SELECT n.id,n.remark,n.node_port,n.status,n.client_address,a.bound,a.applied_revision,a.health_mode,a.probe_status,a.probe_at,a.agent_version,a.core_channel,a.core_version,a.desired_json
        FROM ssh_nodes b JOIN nodes n ON n.id=b.node_id LEFT JOIN node_agents a ON a.node_id=n.id WHERE b.server_id=? ORDER BY n.id''',(s['id'],)).fetchall()
    result['nodes']=[]
    for row in rows:
        n=dict(row);cfg=json.loads(n.pop('desired_json') or '{}');n.update(protocol=cfg.get('protocol','vless-ws-argo'),tunnel_mode=cfg.get('tunnel_mode','temp'))
        n.update(installed=bool(n['bound'] and n['applied_revision']>0),display_name=node_names.format_name(n['remark'],n['protocol']));result['nodes'].append(n)
    result['protocols']=[{'protocol':kind,'name':node_names.LABELS[kind],'count':sum(n['installed'] and n['protocol']==kind for n in result['nodes'])} for kind in node_names.SUFFIXES]
    result['components']={'ssh':'ready' if s['credentials'] else 'unconfigured',
      'probe':'online' if monitor and monitor['online'] else 'offline' if monitor else 'not_installed',
      'protocols_ok':sum(n['probe_status']=='ok' and agent.age(n['probe_at'])<600 for n in rows),'protocols_total':len(rows)}
    return result


class Settings(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    group_name:str=Field(default='',max_length=40)
    notes:str=Field(default='',max_length=2000)
    control_url:str=Field(default='',max_length=500)
    fallback_urls:list[str]=Field(default_factory=list,max_length=3)


class NewNode(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    protocol:str=Field(default='vless-ws-argo',pattern=r'^(vless-ws-argo|reality|hysteria2)$')
    node_port:int=Field(ge=1,le=65535)
    client_address:str=Field(default='',max_length=253)
    client_port:int=Field(default=443,ge=1,le=65535)
    tunnel_mode:str=Field(default='fixed',pattern=r'^(temp|fixed)$')
    fixed_hostname:str=Field(default='',max_length=253)
    server_name:str=Field(default='www.amd.com',max_length=253)
    auto_install:bool=True
    install_probe:bool=False


def register(app,owner):
    @app.post('/api/servers/{server_id}/nodes')
    def add_node(server_id:str,body:NewNode,request:Request):
        owner(request)
        if body.install_probe:raise HTTPException(410,'独立资源探针已移除')
        from . import ssh_control,recovery,agent,monitor,controlplane,labels
        with agent.LOCK:
            s=ssh_control.server(server_id)
            if recovery.busy(server_id) or ssh_control.busy(server_id):raise HTTPException(409,'服务器有未完成任务，请等待完成后再新增节点')
            existing=ssh_control.node_ids(s)
            for nid in existing:
                a=agent.row(nid)
                if not a:continue
                protocol=json.loads(a['desired_json']).get('protocol','vless-ws-argo')
                # UDP Hysteria2 can share a numeric port with TCP, but never the same transport.
                if a['actual_port']==body.node_port and (protocol=='hysteria2')==(body.protocol=='hysteria2'):
                    raise HTTPException(409,'该服务器上已有节点占用此协议传输类型的监听端口')
                if db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(nid,)).fetchone():
                    raise HTTPException(409,'同机有未完成配置任务，请等待完成')
            if body.auto_install and not s['credentials']:raise HTTPException(409,'自动安装前请先配置 SSH；也可取消自动安装获取手动命令')
            core=agent.row(existing[0])['core_channel'] if existing and agent.row(existing[0]) else 'minimal'
            if core not in ('minimal','original'):core='minimal'
            config=agent.CreateBody(remark=body.name,protocol=body.protocol,node_port=body.node_port,
                client_address=body.client_address,client_port=body.client_port,core_channel=core,
                tunnel_mode=body.tunnel_mode,fixed_hostname=body.fixed_hostname,server_name=body.server_name,
                management_enabled=True,install_monitor=False)
            result=agent.create_node(config);nid=result['id'];c=db.get_conn()
            c.execute('INSERT INTO ssh_nodes VALUES (?,?)',(nid,server_id))
            if s['monitor_host_id']:c.execute('INSERT INTO monitor_nodes VALUES (?,?)',(nid,s['monitor_host_id']))
            c.commit()
            # Regenerate after association so bootstrap inherits this server's trusted origins.
            result=agent.enrollment(nid)
            if body.auto_install:
                run=recovery.create(server_id,recovery.Start(node_ids=[nid],confirmed=True,install_probe=body.install_probe,
                    verify_probe=False))
                return {'id':nid,'recovery':run,'reuses_probe':False}
            return result|{'reuses_probe':False}

    @app.get('/api/servers')
    def listing(request:Request):
        owner(request)
        from . import ssh_control
        ssh_control.sync_hosts()
        return [describe(dict(r)) for r in db.get_conn().execute('SELECT * FROM ssh_servers ORDER BY name')]

    @app.get('/api/servers/{server_id}')
    def details(server_id:str,request:Request):
        owner(request)
        from . import ssh_control
        return describe(ssh_control.server(server_id))

    @app.put('/api/servers/{server_id}')
    def settings(server_id:str,body:Settings,request:Request):
        owner(request)
        from . import ssh_control,recovery,agent
        with agent.LOCK:
            s=ssh_control.server(server_id)
            if ssh_control.busy(server_id) or recovery.busy(server_id,include_paused=False):raise HTTPException(409,'服务器有未完成任务，请先完成后修改管理通道')
            primary=controlplane.clean(body.control_url);fallback=[controlplane.clean(v) for v in body.fallback_urls]
            if any(not v for v in fallback) or len(set(fallback))!=len(fallback) or primary in fallback:raise HTTPException(422,'备用管理地址不能为空或重复')
            db.get_conn().execute('UPDATE ssh_servers SET name=?,group_name=?,notes=?,control_url=?,fallback_urls=? WHERE id=?',
                (body.name.strip(),body.group_name.strip(),body.notes,primary,json.dumps(fallback),server_id));db.get_conn().commit()
            if s['monitor_host_id']:
                db.get_conn().execute('UPDATE monitor_hosts SET name=?,group_name=? WHERE id=?',(body.name.strip(),body.group_name.strip(),s['monitor_host_id']));db.get_conn().commit()
            return describe(ssh_control.server(server_id))
