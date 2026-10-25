"""Read adapters for durable queues, normalized errors and bounded progress."""
from datetime import datetime,timezone
from fastapi import HTTPException,Request,Query
from . import db

LABELS={'install':'安装协议','read':'读取配置','update':'修改配置','upgrade':'更新管理脚本','uninstall':'卸载节点','fixed':'固定隧道迁移',
        'core':'更新核心','core_rollback':'核心回滚','monitor_install':'安装探针','diagnose':'只读诊断','preflight':'系统能力检测',
        'agent_tick':'执行节点任务','custom':'自定义维护','restart':'重启服务','install_key':'配置管理密钥','revoke_key':'撤销管理密钥',
        'switch_ssh':'切换 SSH 管理','switch_agent':'切回主动拉取','bootstrap':'首次安装','recover_node':'恢复协议','recover_probe':'恢复探针','control_sync':'更新管理地址'}


def init():
    from . import schema
    schema.columns('ssh_tasks',[('phase',"TEXT NOT NULL DEFAULT 'queued'"),('updated','REAL NOT NULL DEFAULT 0')])


def error_info(message,status):
    from . import ssh_control
    message=ssh_control.redact(message)
    if not message:return None
    if status=='unknown':code='unknown_state'
    elif any(s in message for s in ('指纹','身份')):code='host_identity'
    elif any(s in message for s in ('认证','凭据','密码','密钥')):code='authentication'
    elif '超时' in message:code='timeout'
    elif any(s in message for s in ('连接','中转','不可达')):code='network'
    elif any(s in message for s in ('系统','架构','不满足','不支持')):code='unsupported'
    else:code='execution'
    return {'code':code,'message':message,'retry_after_review':code in ('timeout','network','execution') and status!='unknown'}


def stamp(value):
    if isinstance(value,(int,float)):return value
    if not value:return 0
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except ValueError:return 0


def listing(kind='',status='',server_id='',limit=50,offset=0):
    from . import ssh_control
    c=db.get_conn();items=[]
    for r in c.execute('''SELECT t.id,t.node_id,t.action,t.status,t.created,t.finished,t.error,t.phase,t.updated,t.server_id,s.name
        FROM ssh_tasks t JOIN ssh_servers s ON s.id=t.server_id WHERE (?='' OR t.server_id=?) ORDER BY t.created DESC LIMIT 300''',(server_id,server_id)):
        x=dict(r);items.append({'id':'ssh:'+x['id'],'source_id':x['id'],'kind':'ssh','name':x['name']+' · '+LABELS.get(x['action'],x['action']),'server_id':x['server_id'],
          'node_id':x['node_id'],'status':x['status'],'phase':x['phase'],'created':x['created'],'finished':x['finished'],'error':x['error']})
    for r in c.execute('''SELECT j.node_id,j.revision,j.operation,j.status,j.created_at,j.finished_at,j.error,n.remark,b.server_id,a.last_job_error,a.desired_revision
        FROM agent_jobs j JOIN nodes n ON n.id=j.node_id LEFT JOIN ssh_nodes b ON b.node_id=j.node_id LEFT JOIN node_agents a ON a.node_id=j.node_id
        WHERE (?='' OR b.server_id=?) ORDER BY j.created_at DESC,j.revision DESC LIMIT 300''',(server_id,server_id)):
        x=dict(r);items.append({'id':f"node:{x['node_id']}:{x['revision']}",'source_id':f"{x['node_id']}:{x['revision']}",'kind':'node','name':x['remark']+' · '+LABELS.get(x['operation'],x['operation']),
          'server_id':x['server_id'] or '', 'node_id':x['node_id'],'status':x['status'],'phase':x['operation'],'created':stamp(x['created_at']),
          'finished':stamp(x['finished_at']),'error':(x['error'] or (x['last_job_error'] if x['revision']==x['desired_revision'] else '历史任务失败，详情请查看当时日志')) if x['status']=='failed' else ''})
    for r in c.execute('''SELECT b.*,(SELECT count(*) FROM upgrade_items i WHERE i.batch_id=b.id AND i.status='done') complete,
        (SELECT count(*) FROM upgrade_items i WHERE i.batch_id=b.id) total FROM upgrade_batches b WHERE (?='' OR EXISTS (SELECT 1 FROM upgrade_items i JOIN ssh_nodes n ON n.node_id=i.node_id WHERE i.batch_id=b.id AND n.server_id=?)) ORDER BY b.id DESC LIMIT 100''',(server_id,server_id)):
        x=dict(r)
        items.append({'id':'upgrade:'+str(x['id']),'source_id':str(x['id']),'kind':'upgrade','name':'分批升级 · '+x['kind'],'server_id':server_id,
          'node_id':None,'status':x['status'],'phase':f"{x['complete']}/{x['total']}",'created':stamp(x['created_at']),'finished':0,'error':x['message'] if x['status']=='paused' else ''})
    for r in c.execute("SELECT r.id,r.server_id,r.status,r.created,r.finished,r.error,r.position,s.name FROM recovery_runs r JOIN ssh_servers s ON s.id=r.server_id WHERE (?='' OR r.server_id=?) ORDER BY r.created DESC LIMIT 100",(server_id,server_id)):
        x=dict(r);items.append({'id':'recovery:'+x['id'],'source_id':x['id'],'kind':'recovery','name':x['name']+' · 重装恢复','server_id':x['server_id'],
          'node_id':None,'status':x['status'],'phase':str(x['position']+1),'created':x['created'],'finished':x['finished'],'error':x['error']})
    items.sort(key=lambda x:x['created'],reverse=True)
    stats={}
    for x in items:stats[x['status']]=stats.get(x['status'],0)+1
    selected=[x for x in items if (not kind or x['kind']==kind) and (not status or x['status']==status) and (not server_id or x['server_id']==server_id)]
    for x in selected:
        x['error']=ssh_control.redact(x['error']);x['error_info']=error_info(x['error'],x['status'])
    return {'items':selected[offset:offset+limit],'total':len(selected),'stats':stats,'retained_window':True}


def register(app,owner):
    @app.get('/api/tasks')
    def tasks(request:Request,kind:str='',status:str='',server_id:str='',limit:int=Query(default=50,ge=1,le=100),offset:int=Query(default=0,ge=0,le=1000)):
        owner(request);return listing(kind,status,server_id,limit,offset)

    @app.get('/api/tasks/{task_id}/output')
    def output(task_id:str,request:Request,server_id:str=''):
        owner(request)
        from . import ssh_control
        if task_id.startswith('upgrade:'):
            c=db.get_conn();batch=c.execute('SELECT kind,status,message FROM upgrade_batches WHERE id=?',(task_id[8:],)).fetchone()
            if batch:
                items=[dict(i) for i in c.execute('SELECT node_id,name,status,message FROM upgrade_items WHERE batch_id=? ORDER BY position',(task_id[8:],))]
                if server_id:
                    owned={r['node_id'] for r in db.get_conn().execute('SELECT node_id FROM ssh_nodes WHERE server_id=?',(server_id,))}
                    items=[i for i in items if i['node_id'] in owned]
                    if not items:raise HTTPException(404,'该服务器不在此批次中')
                return {'status':batch['status'],'phase':batch['kind'],'error':ssh_control.redact(batch['message']),
                  'result':ssh_control.redact('\n'.join(f"{i['name']} · {i['status']} · {i['message']}" for i in items))}
        if task_id.startswith('ssh:'):
            r=db.get_conn().execute('SELECT result,error,status,phase,updated FROM ssh_tasks WHERE id=?',(task_id[4:],)).fetchone()
            if r:return dict(r)|{'error_info':error_info(r['error'],r['status'])}
        if task_id.startswith('node:'):
            try:nid,revision=map(int,task_id[5:].split(':'))
            except ValueError:raise HTTPException(404,'任务不存在') from None
            r=db.get_conn().execute('SELECT * FROM agent_jobs WHERE node_id=? AND revision=?',(nid,revision)).fetchone()
            if r:
                lines=cached_node_output(nid,r['created_at'],r['finished_at'])
                return {'result':lines,'status':r['status'],'phase':r['operation'],'error':ssh_control.redact(r['error']),'error_info':error_info(r['error'],r['status'])}
        raise HTTPException(404,'任务不存在或请在升级/恢复详情中查看')


def cached_node_output(node_id,started,finished):
    from . import ssh_control
    rows=db.get_conn().execute("SELECT line FROM logs WHERE node_id=? AND created_at>=? AND (?='' OR created_at<=?) ORDER BY id DESC LIMIT 100",(node_id,started,finished,finished)).fetchall()
    return ssh_control.redact('\n'.join(r['line'] for r in reversed(rows)))
