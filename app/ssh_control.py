"""On-demand SSH management with pinned hosts, encrypted credentials and durable jobs."""
import base64
import hashlib
import io
import json
import re
import secrets
import shlex
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4
import paramiko
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException, Request
from pydantic import Field, SecretStr
from . import db, deploy, operations, events

ACTIVE=set()
LOCK=threading.Lock()
REMOTE='/var/lib/argo-panel-ssh'
RELAY_LOCAL=threading.local()


@contextmanager
def relay_runtime(link):
    from . import proxy_runtime
    pool=getattr(RELAY_LOCAL,'pool',None)
    if pool is None:pool=RELAY_LOCAL.pool={}
    key=hashlib.sha256(link.encode()).hexdigest()
    if key not in pool:
        context=proxy_runtime.runtime(link,threading.Event());port=context.__enter__()
        pool[key]={'context':context,'port':port,'references':0}
    entry=pool[key];entry['references']+=1
    try:yield entry['port']
    finally:
        entry['references']-=1
        if not entry['references']:
            pool.pop(key,None);entry['context'].__exit__(None,None,None)


def cipher():
    secret=(db.DATA_DIR/'secret.key').read_bytes().strip()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'argo-ssh-v1:'+secret).digest()))


def seal(value):return cipher().encrypt(json.dumps(value).encode()).decode()
def unseal(value):return json.loads(cipher().decrypt(value.encode()))


def init():
    c=db.get_conn();c.executescript('''
    CREATE TABLE IF NOT EXISTS ssh_servers(id TEXT PRIMARY KEY,name TEXT NOT NULL,
      host TEXT NOT NULL DEFAULT '',port INTEGER NOT NULL DEFAULT 22,host_key TEXT NOT NULL DEFAULT '',
      credentials TEXT NOT NULL DEFAULT '',auth_kind TEXT NOT NULL DEFAULT '',public_key TEXT NOT NULL DEFAULT '',
      monitor_host_id TEXT UNIQUE REFERENCES monitor_hosts(id) ON DELETE SET NULL,
      mode TEXT NOT NULL DEFAULT 'agent',verified_at TEXT NOT NULL DEFAULT '',last_error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ssh_nodes(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      server_id TEXT NOT NULL REFERENCES ssh_servers(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS ssh_tasks(id TEXT PRIMARY KEY,server_id TEXT NOT NULL REFERENCES ssh_servers(id) ON DELETE CASCADE,
      node_id INTEGER,action TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'queued',
      created REAL NOT NULL,deadline REAL NOT NULL,next_check REAL NOT NULL DEFAULT 0,started INTEGER NOT NULL DEFAULT 0,
      result TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',finished REAL NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ssh_tasks_status ON ssh_tasks(status,next_check);
    ''')
    from . import schema
    schema.columns('ssh_servers',[('automation_paused', 'INTEGER NOT NULL DEFAULT 0'), ('relay_node_id', 'INTEGER')],version=1)
    c.commit()
    from . import servers
    servers.init()


def sync_hosts():
    c=db.get_conn()
    for h in c.execute('SELECT id,name,group_name FROM monitor_hosts').fetchall():
        r=c.execute('SELECT id FROM ssh_servers WHERE monitor_host_id=?',(h['id'],)).fetchone()
        if not r:
            sid=uuid4().hex
            c.execute('INSERT INTO ssh_servers(id,name,monitor_host_id,created_at,group_name) VALUES (?,?,?,?,?)',(sid,h['name'],h['id'],db.utcnow(),h['group_name']))
        else:sid=r['id']
        # Initial associations follow the existing probe mapping. Later edits are explicit.
        if not r:c.execute('INSERT OR IGNORE INTO ssh_nodes SELECT node_id,? FROM monitor_nodes WHERE host_id=?',(sid,h['id']))
    c.commit()


def server(server_id):
    r=db.get_conn().execute('SELECT * FROM ssh_servers WHERE id=?',(server_id,)).fetchone()
    if not r:raise HTTPException(404,'服务器不存在')
    return dict(r)


def binding(node_id):
    if not db.get_conn().execute("SELECT 1 FROM sqlite_master WHERE name='ssh_nodes'").fetchone():return None
    r=db.get_conn().execute('SELECT s.* FROM ssh_servers s JOIN ssh_nodes n ON n.server_id=s.id WHERE n.node_id=?',(node_id,)).fetchone()
    return dict(r) if r else None


def controls(node_id):
    s=binding(node_id);return bool(s and s['mode']=='ssh')


def public(s):
    from . import node_names
    key=s['host_key']
    fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(base64.b64decode(key)).digest()).decode().rstrip('=') if key else ''
    nodes=[]
    for r in db.get_conn().execute('SELECT n.id,n.remark,n.share_link,a.desired_json FROM nodes n JOIN ssh_nodes b ON b.node_id=n.id LEFT JOIN node_agents a ON a.node_id=n.id WHERE b.server_id=?',(s['id'],)):
        cfg=json.loads(r['desired_json'] or '{}');kind=cfg.get('protocol') or node_names.protocol(dict(r))
        nodes.append({'id':r['id'],'remark':r['remark'],'protocol':kind,'display_name':node_names.format_name(r['remark'],kind)})
    relay=db.get_node(s['relay_node_id']) if s.get('relay_node_id') else None
    return {k:s[k] for k in ('id','name','host','port','mode','auth_kind','verified_at','last_error','monitor_host_id','automation_paused','relay_node_id')}|{
      'relay_name':relay['remark'] if relay else '',
      'configured':bool(s['credentials']),'fingerprint':fingerprint,'host_key':key,'managed_key':bool(s['public_key']),'nodes':nodes}


def failure(exc):
    if isinstance(exc,HTTPException):return str(exc.detail)
    if isinstance(exc,HostChanged):return 'SSH 主机指纹已变化，请核对是否重装或 DDNS 指向了其他服务器'
    if isinstance(exc,paramiko.AuthenticationException):return 'SSH 认证失败，请在面板更新 root 密码或私钥'
    if isinstance(exc,(TimeoutError,TimeoutErrorSSH)):return 'SSH 连接或执行超时，请检查地址、端口和网络'
    if isinstance(exc,BindingMismatch):return '远端节点身份与面板不一致，未执行操作；请检查服务器绑定'
    if isinstance(exc,RelayUnavailable):return 'SSH 中转节点不可用，请检查中转节点或更换连接通道'
    return 'SSH 操作未完成（'+type(exc).__name__+'），请检查连接及任务状态'


class HostChanged(Exception):pass
class BindingMismatch(Exception):pass
class TimeoutErrorSSH(Exception):pass
class RelayUnavailable(Exception):pass


def relay_node(relay_node_id,server_id=None):
    if not relay_node_id:return None
    n=db.get_node(relay_node_id)
    if not n or not n.get('share_link') or n.get('status')=='uninstalling':raise RelayUnavailable()
    if server_id and db.get_conn().execute('SELECT 1 FROM ssh_nodes WHERE server_id=? AND node_id=?',(server_id,relay_node_id)).fetchone():
        raise HTTPException(409,'中转节点不能是当前服务器上的关联节点，请选择其他服务器的节点')
    return n


class RelayTransport:
    """Own both the SSH transport and its short-lived panel-side proxy process."""
    def __init__(self,transport,context):self.transport=transport;self.context=context;self.closed=False
    def __getattr__(self,name):return getattr(self.transport,name)
    @property
    def channel_timeout(self):return self.transport.channel_timeout
    @channel_timeout.setter
    def channel_timeout(self,value):self.transport.channel_timeout=value
    def close(self):
        if self.closed:return
        self.closed=True
        try:self.transport.close()
        finally:self.context.__exit__(None,None,None)


def open_transport(s):
    if not s.get('relay_node_id'):return deploy.transport(deploy.Target(host=s['host'],ssh_port=s['port']))
    n=relay_node(s['relay_node_id'],s.get('id'))
    context=relay_runtime(n['share_link']);sock=None;t=None
    try:port=context.__enter__()
    except Exception:raise RelayUnavailable() from None
    try:
        sock=socket.create_connection(('127.0.0.1',port),timeout=5);sock.settimeout(15)
        host='['+s['host']+']' if ':' in s['host'] else s['host'];address=host+':'+str(s['port'])
        sock.sendall(f'CONNECT {address} HTTP/1.1\r\nHost: {address}\r\n\r\n'.encode())
        headers=b''
        while not headers.endswith(b'\r\n\r\n') and len(headers)<4096:
            chunk=sock.recv(1)
            if not chunk:break
            headers+=chunk
        if not headers.endswith(b'\r\n\r\n') or not re.match(rb'^HTTP/1\.[01] 200(?: |\r)',headers):raise RelayUnavailable()
        t=paramiko.Transport(sock);t.auth_timeout=20;t.banner_timeout=15;t.start_client(timeout=15)
        return RelayTransport(t,context)
    except Exception:
        try:
            if t:t.close()
            elif sock:sock.close()
        finally:context.__exit__(None,None,None)
        raise RelayUnavailable() from None


def connect(s,credentials=None):
    t=open_transport(s)
    try:
        t.channel_timeout=20
        if not secrets.compare_digest(t.get_remote_server_key().get_base64(),s['host_key']):raise HostChanged()
        cred=credentials if credentials is not None else unseal(s['credentials'])
        if cred.get('private_key'):
            key=None
            for kind in (paramiko.Ed25519Key,paramiko.RSAKey,paramiko.ECDSAKey):
                try:key=kind.from_private_key(io.StringIO(cred['private_key']),password=cred.get('passphrase') or None);break
                except (paramiko.SSHException,ValueError):continue
            if key is None:raise paramiko.AuthenticationException()
            t.auth_publickey('root',key)
        else:deploy.authenticate_password(t,'root',cred.get('password',''))
        if not t.is_authenticated():raise paramiko.AuthenticationException()
        code,out=command(t,'id -u',10)
        if code or out.strip()!='0':raise paramiko.AuthenticationException()
        return t
    except Exception:t.close();raise


def command(t,text,timeout=20):
    ch=t.open_session(timeout=10);ch.settimeout(10);ch.exec_command(text)
    end=time.monotonic()+timeout;data=bytearray()
    try:
        while True:
            for ready,recv in ((ch.recv_ready,ch.recv),(ch.recv_stderr_ready,ch.recv_stderr)):
                if ready():
                    chunk=recv(8192)
                    if len(data)<65536:data.extend(chunk[:65536-len(data)])
            if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():return ch.recv_exit_status(),data.decode(errors='replace')
            if time.monotonic()>end:raise TimeoutErrorSSH()
            time.sleep(.05)
    finally:ch.close()


def read(sftp,path,limit=65536):
    with sftp.open(path,'rb') as f:
        data=f.read(limit+1)
    if len(data)>limit:raise ValueError('oversize')
    return data.decode(errors='replace')


def exists(sftp,path):
    try:sftp.stat(path);return True
    except IOError:return False


def write(sftp,path,text,mode=0o600):
    with sftp.open(path+'.new','wb') as f:f.write(text.encode())
    sftp.chmod(path+'.new',mode)
    try:sftp.posix_rename(path+'.new',path)
    except IOError:
        if exists(sftp,path):raise
        sftp.rename(path+'.new',path)


def verify_nodes(sftp,s,node_id=None):
    from . import agent
    ids=[node_id] if node_id else [r[0] for r in db.get_conn().execute('SELECT node_id FROM ssh_nodes WHERE server_id=?',(s['id'],))]
    for nid in ids:
        a=agent.row(nid)
        if not a or not a['bound']:raise BindingMismatch()
        text=read(sftp,f'/usr/local/etc/argo-deploy/node-{nid}/curl.conf',4096)
        match=re.search(r'X-Agent-Token: ([A-Za-z0-9_-]+)',text)
        if not match or agent.digest(match[1])!=a['token_hash']:raise BindingMismatch()


def redact(text):
    text=re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',text)
    text=re.sub(r'(?im)^.*(?:SB_WEB_RESULT=|X-Agent-Token|authorization:|private.?key|api.?token|password|passwd|tunnel.?token).*$','[敏感行已隐藏]',text)
    text=re.sub(r'(?:vless|hysteria2|hy2|trojan|ss|vmess)://\S+','[节点链接已隐藏]',text)
    text=re.sub(r'https?://\S+','[URL 已隐藏]',text)
    return text[-24000:]


def busy(server_id):
    return bool(db.get_conn().execute("SELECT 1 FROM ssh_tasks WHERE server_id=? AND status IN ('queued','running','unknown')",(server_id,)).fetchone())


def guard_enqueue(node_id):
    s=binding(node_id)
    if s and db.get_conn().execute("SELECT 1 FROM ssh_tasks WHERE server_id=? AND status IN ('queued','running','unknown') AND action IN ('switch_ssh','switch_agent','restart','custom','bootstrap','control_sync')",(s['id'],)).fetchone():
        raise HTTPException(409,'服务器正在执行 SSH 维护或切换管理方式，请先完成该任务')


def queue(server_id,action,node_id=None,payload=None):
    s=server(server_id)
    if not s['credentials']:raise HTTPException(409,'请先在面板填写并验证 SSH 凭据')
    if busy(server_id):raise HTTPException(409,'该服务器有未完成的 SSH 任务，请先查看结果')
    if node_id and not db.get_conn().execute('SELECT 1 FROM ssh_nodes WHERE server_id=? AND node_id=?',(server_id,node_id)).fetchone():raise HTTPException(409,'节点未绑定到该服务器')
    task_id=uuid4().hex;now=time.time()
    db.get_conn().execute('INSERT INTO ssh_tasks(id,server_id,node_id,action,payload,created,deadline) VALUES (?,?,?,?,?,?,?)',
      (task_id,server_id,node_id,action,seal(payload or {}),now,now+1800));db.get_conn().commit()
    return {'id':task_id,'status':'queued'}


def node_ids(s):return [r[0] for r in db.get_conn().execute('SELECT node_id FROM ssh_nodes WHERE server_id=?',(s['id'],))]


def task_script(task,s):
    nid=task['node_id'];action=task['action'];q=shlex.quote
    base='#!/bin/sh\nset -eu\numask 077\nexport PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
    if action=='diagnose':
        return base+'''echo '=== 系统 ==='
uname -srmo
[ ! -r /etc/os-release ] || sed -n '/^PRETTY_NAME=/p' /etc/os-release
printf 'root uid: ';id -u
printf '初始化进程: ';cat /proc/1/comm
printf '可用内存: ';awk '/MemAvailable:/{print $2 " KiB"}' /proc/meminfo
df -h / /var/tmp
printf '能力与限制: ';awk '/^(CapEff|NoNewPrivs|Seccomp):/{print}' /proc/self/status
[ ! -f /proc/user_beancounters ] || echo '容器计数接口存在'
echo '=== 服务进程 ==='
ps -eo pid,comm,rss 2>/dev/null | awk 'NR==1 || /sing-box|cloudflared|monitor-agent|sshd/' || true
echo '=== 监听端口 ==='
ss -lntu 2>/dev/null || netstat -lntu 2>/dev/null || true
'''+''.join(f'\necho "=== 节点 {i} ==="\n[ ! -f /usr/local/lib/argo-deploy/diagnose.sh ] || sh /usr/local/lib/argo-deploy/diagnose.sh {i} || true\n' for i in node_ids(s))
    if action=='agent_tick':
        from . import controlplane,agent
        selection=''
        if s.get('control_url') or json.loads(s.get('fallback_urls','[]')):
            selection=controlplane.runtime_selection(agent.PUBLIC_URL(),s)+f'''jq --arg panel "$CONTROL" '.panel=$panel' /usr/local/etc/argo-deploy/node-{nid}/settings.json > /usr/local/etc/argo-deploy/node-{nid}/settings.json.new
mv /usr/local/etc/argo-deploy/node-{nid}/settings.json.new /usr/local/etc/argo-deploy/node-{nid}/settings.json
'''
        return base+selection+f'''export ARGO_SSH_EXECUTION=1
exec bash -c 'curl() {{ command curl -H "X-SSH-Execution: 1" "$@"; }}; export -f curl; exec bash /usr/local/lib/argo-deploy/agent.sh /usr/local/etc/argo-deploy/node-{nid}'
'''
    if action=='restart':
        return base+f'''exec 6>/run/argo-deploy-job.lock
timeout 30 flock 6
exec 8>/usr/local/etc/argo-deploy/node-{nid}/tick.lock
timeout 30 flock 8
bash /usr/local/lib/argo-deploy/repair.sh /usr/local/etc/argo-deploy/node-{nid} service 8>&- 6>&-
'''
    if action in ('bootstrap','recover_node','recover_probe'):return unseal(task['payload'])['script']
    if action=='control_sync':
        from . import controlplane,agent
        script=base+controlplane.runtime_selection(agent.PUBLIC_URL(),s)+'''exec 6>/run/argo-deploy-job.lock
timeout 30 flock 6
echo ARGO_STAGE=control_update
'''
        for i in node_ids(s):
            script+=f'''timeout 30 flock /usr/local/etc/argo-deploy/node-{i}/tick.lock sh -c 'jq --arg panel "$1" ".panel=\\$panel" "$2/settings.json" > "$2/settings.json.new" && chmod 600 "$2/settings.json.new" && mv "$2/settings.json.new" "$2/settings.json"' control-sync "$CONTROL" /usr/local/etc/argo-deploy/node-{i}
'''
        script+='''if [ -s /usr/local/etc/argo-monitor/environment ];then
 awk -v server="$CONTROL/monitor" '/^MONITOR_SERVER=/ {$0="MONITOR_SERVER="server} {print}' /usr/local/etc/argo-monitor/environment > /usr/local/etc/argo-monitor/environment.new
 chmod 600 /usr/local/etc/argo-monitor/environment.new
 mv /usr/local/etc/argo-monitor/environment.new /usr/local/etc/argo-monitor/environment
 if [ -d /run/systemd/system ];then systemctl restart argo-monitor;elif command -v rc-service >/dev/null;then rc-service argo-monitor restart;fi
fi
echo '管理脚本与探针已更新到通过连通检查的管理地址'
'''
        return script
    if action=='custom':
        guards=''.join(f'[ ! -f /usr/local/etc/argo-deploy/node-{i}/tick.lock ] || timeout 30 flock /usr/local/etc/argo-deploy/node-{i}/tick.lock true\n' for i in node_ids(s))
        return base+'exec 6>/run/argo-deploy-job.lock\ntimeout 30 flock 6\n'+guards+'(\nexec 6>&-\n'+unseal(task['payload'])['script']+'\n)\n'
    if action in ('switch_ssh','switch_agent'):
        ids=node_ids(s)
        if not ids:raise BindingMismatch()
        script=base+'T=$(mktemp);trap \'rm -f "$T"\' EXIT\n(crontab -l 2>/dev/null || true) > "$T"\n'
        # Acquire each one-shot profile lock before changing scheduling.
        for index,i in enumerate(ids):
            # flock -c avoids shell FD limits on hosts with many profiles.
            script+=f'''timeout 30 flock /usr/local/etc/argo-deploy/node-{i}/tick.lock true
jq '.management_enabled=true' /usr/local/etc/argo-deploy/node-{i}/settings.json > /usr/local/etc/argo-deploy/node-{i}/settings.json.new
mv /usr/local/etc/argo-deploy/node-{i}/settings.json.new /usr/local/etc/argo-deploy/node-{i}/settings.json
sed -i '/# argo-deploy-node-{i}$/d' "$T"
'''
            if action in ('switch_agent','switch_ssh'):script+=f'printf \'%s\\n\' \'* * * * * sh /usr/local/lib/argo-deploy/agent.sh /usr/local/etc/argo-deploy/node-{i} >/dev/null 2>&1 # argo-deploy-node-{i}\' >> "$T"\n'
        script+='crontab "$T"\necho "管理任务调度已更新；协议和资源探针保持运行"\n'
        return script
    raise ValueError('unsupported action')


def finish(task,status,result='',error=''):
    c=db.get_conn();c.execute('UPDATE ssh_tasks SET status=?,result=?,error=?,finished=?,payload=\'\' WHERE id=?',(status,redact(result),error,time.time(),task['id']))
    c.execute('UPDATE ssh_tasks SET phase=?,updated=? WHERE id=?',(status,time.time(),task['id']))
    c.execute('UPDATE ssh_servers SET last_error=? WHERE id=?',(error,task['server_id']))
    if task['node_id'] and db.get_node(task['node_id']):events.add(task['node_id'],'ssh','SSH '+task['action']+(' 执行完成' if status=='done' else ' 执行未完成，请查看 SSH 任务'),'info' if status=='done' else 'warning')
    if status=='done' and task['action'] in ('switch_ssh','switch_agent'):
        mode='ssh' if task['action']=='switch_ssh' else 'agent'
        c.execute('UPDATE ssh_servers SET mode=? WHERE id=?',(mode,task['server_id']))
        for nid in node_ids(server(task['server_id'])):
            c.execute('UPDATE node_agents SET management_enabled=1 WHERE node_id=?',(nid,))
            events.add(nid,'ssh','管理方式切换为 '+('SSH 按需执行' if mode=='ssh' else '节点主动拉取'))
    if status=='failed':c.execute('UPDATE ssh_servers SET automation_paused=1 WHERE id=?',(task['server_id'],))
    c.commit()
    try:notify(task,status)
    except Exception:pass


def notify(task,status):
    from . import telemetry
    cfg=telemetry.config()
    if not cfg.get('enabled') or not cfg.get('alerts_tasks',True):return
    if task['action']=='agent_tick' and status=='done':return
    s=server(task['server_id'])
    labels={'diagnose':'只读诊断','preflight':'系统能力检测','recover_node':'恢复协议','recover_probe':'恢复资源探针','agent_tick':'节点任务','custom':'自定义维护','restart':'重启协议服务',
            'install_key':'管理密钥配置','revoke_key':'管理密钥撤销','bootstrap':'首次安装','switch_ssh':'切换 SSH 管理','switch_agent':'恢复主动拉取'}
    telemetry.enqueue('ssh:'+task['id']+':'+status,telemetry.message_text('✅ SSH 任务完成' if status=='done' else '⚠️ SSH 任务需要处理',
      [('服务器',s['name']),('操作',labels.get(task['action'],'维护任务')),('结果','完成' if status=='done' else '失败或结果未知，自动运维已暂停')],
      '请在 SSH 运维页查看记录；未知结果不会自动重放命令。'),category='tasks',route='ssh-module',priority=1 if status=='done' else 2)
    db.get_conn().commit()


def uncertain(task,message,delay=60):
    c=db.get_conn()
    c.execute("UPDATE ssh_tasks SET status='unknown',error=?,next_check=? WHERE id=?",(message,time.time()+delay,task['id']))
    c.execute('UPDATE ssh_servers SET automation_paused=1,last_error=? WHERE id=?',(message,task['server_id']));c.commit()
    try:notify(task,'unknown')
    except Exception:pass


def key_task(t,sftp,task,s):
    payload=unseal(task['payload'])
    if not payload.get('public_key'):
        key=Ed25519PrivateKey.generate()
        payload={'private_key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.OpenSSH,serialization.NoEncryption()).decode(),
                 'public_key':key.public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()+' argo-panel:'+s['id']}
        db.get_conn().execute('UPDATE ssh_tasks SET payload=? WHERE id=?',(seal(payload),task['id']));db.get_conn().commit()
    code,home=command(t,'printf "%s" "$HOME"')
    if code or not re.fullmatch(r'/[A-Za-z0-9_./-]+',home):raise ValueError('home')
    directory=home+'/.ssh';path=directory+'/authorized_keys'
    command(t,'mkdir -p '+shlex.quote(directory)+' && chmod 700 '+shlex.quote(directory))
    old=read(sftp,path,128*1024) if exists(sftp,path) else ''
    line='restrict '+payload['public_key']
    if payload['public_key'] not in old:write(sftp,path,old.rstrip()+'\n'+line+'\n')
    new_cred={'private_key':payload['private_key']}
    try:
        check=connect(s,new_cred);check.close()
    except Exception:
        try:
            current=read(sftp,path,128*1024)
            write(sftp,path,'\n'.join(line for line in current.splitlines() if payload['public_key'] not in line)+'\n')
        except Exception:pass
        raise
    # Replace only our previously owned key after the new key has authenticated.
    if s['public_key'] and s['public_key']!=payload['public_key']:
        current=read(sftp,path,128*1024)
        write(sftp,path,'\n'.join(line for line in current.splitlines() if s['public_key'] not in line)+'\n')
    c=db.get_conn();c.execute("UPDATE ssh_servers SET credentials=?,auth_kind='managed_key',public_key=?,verified_at=? WHERE id=?",(seal(new_cred),payload['public_key'],db.utcnow(),s['id']));c.commit()
    finish(task,'done','独立管理密钥已安装并通过实际认证；原有登录方式保留，首次密码已从面板配置移除。')


def work(task_id):
    t=None;sftp=None
    try:
        task=dict(db.get_conn().execute('SELECT * FROM ssh_tasks WHERE id=?',(task_id,)).fetchone());s=server(task['server_id'])
        db.get_conn().execute('UPDATE ssh_tasks SET phase=?,updated=? WHERE id=?',('connecting' if not task['started'] else 'collecting',time.time(),task_id));db.get_conn().commit()
        t=connect(s);sftp=paramiko.SFTPClient.from_transport(t);sftp.get_channel().settimeout(20)
        if task['action']=='preflight':
            from . import capabilities
            data=capabilities.inspect(t,s['id']);finish(task,'done',json.dumps(data,ensure_ascii=False));return
        if task['action']=='install_key':key_task(t,sftp,task,s);return
        if task['action']=='revoke_key':
            code,home=command(t,'printf "%s" "$HOME"')
            if code or not re.fullmatch(r'/[A-Za-z0-9_./-]+',home):raise ValueError('home')
            if s['public_key']:
                path=home+'/.ssh/authorized_keys';current=read(sftp,path,128*1024)
                write(sftp,path,'\n'.join(line for line in current.splitlines() if s['public_key'] not in line)+'\n')
            c=db.get_conn();c.execute("UPDATE ssh_servers SET credentials=CASE WHEN auth_kind='managed_key' THEN '' ELSE credentials END,auth_kind=CASE WHEN auth_kind='managed_key' THEN '' ELSE auth_kind END,public_key='',automation_paused=1 WHERE id=?",(s['id'],));c.commit()
            finish(task,'done','已撤销面板安装的密钥；其他登录密钥保持原样。');return
        folder=REMOTE+'/'+task_id
        code,_=command(t,'mkdir -p '+q(folder)+' && chmod 700 '+q(REMOTE)+' '+q(folder))
        if code:raise ValueError('directory')
        if exists(sftp,folder+'/result.rc'):
            rc=int(read(sftp,folder+'/result.rc',32).strip())
            output=remote_output(sftp,folder)
            finish(task,'done' if rc==0 else 'failed',output,'' if rc==0 else '远端任务退出码 '+str(rc))
            if rc==0 and task['node_id']:
                from . import agent
                agent.schedule_probe(task['node_id'])
            # Credentials and raw output are not retained on the remote after acknowledgement.
            for name in ('task.sh','runner.sh','pid','output.log','output.prev','output.pipe','result.rc','result.rc.new'):
                try:sftp.remove(folder+'/'+name)
                except IOError:pass
            try:sftp.rmdir(folder+'/started');sftp.rmdir(folder)
            except IOError:pass
            return
        if exists(sftp,folder+'/started'):
            # Never relaunch a command with unknown completion after reconnect.
            if time.time()>=task['deadline']:uncertain(task,'远端结果未知，已停止后续任务；可重新检查');return
            output=redact(remote_output(sftp,folder));stages=re.findall(r'(?m)^ARGO_STAGE=([a-z_]{1,40})$',output)
            db.get_conn().execute("UPDATE ssh_tasks SET status='running',started=1,next_check=?,error='',result=?,phase=?,updated=? WHERE id=?",(time.time()+10,output,stages[-1] if stages else 'executing',time.time(),task_id));db.get_conn().commit();return
        if task['started']:
            uncertain(task,'远端执行标记缺失，未自动重放；请检查结果');return
        if time.time()>task['deadline']:
            finish(task,'failed',error='排队超时，远端任务未启动');return
        if task['action'] in ('agent_tick','restart','switch_ssh','switch_agent','control_sync'):verify_nodes(sftp,s,task['node_id'])
        if task['action'] in ('bootstrap','recover_node'):
            from . import capabilities
            capabilities.require_install(capabilities.inspect(t,s['id']))
        script=task_script(task,s)
        write(sftp,folder+'/task.sh',script)
        runner='''#!/bin/sh
set -u
umask 077
cd "'''+folder+'''" || exit 1
mkdir started 2>/dev/null || exit 0
printf '%s\n' "$$" > pid
export ARGO_SSH_TASK_ID='''+task_id+'''
mkfifo output.pipe
# mawk otherwise buffers pipe input even when output is explicitly flushed.
awk_option=''
if awk -W version 2>&1 | grep -qi mawk;then awk_option='-W interactive';fi
LC_ALL=C timeout 1530 awk $awk_option 'BEGIN {n=0} {s=substr($0,1,8192);if(n+length(s)+1>65536){close("output.log");system("mv -f output.log output.prev");n=0} print s >> "output.log";fflush("output.log");n+=length(s)+1}' < output.pipe &
tail_pid=$!
timeout 1500 sh task.sh > output.pipe 2>&1
code=$?
wait "$tail_pid" || true
printf '%s\n' "$code" > result.rc.new
mv result.rc.new result.rc
'''
        write(sftp,folder+'/runner.sh',runner)
        # Persist uncertainty BEFORE the launch; remote mkdir makes retries idempotent.
        db.get_conn().execute("UPDATE ssh_tasks SET status='running',started=1,next_check=? WHERE id=?",(time.time()+15,task_id));db.get_conn().commit()
        code,_=command(t,'nohup sh '+q(folder+'/runner.sh')+' >/dev/null 2>&1 < /dev/null &',10)
        if code:raise RuntimeError('launch')
    except Exception as exc:
        task=db.get_conn().execute('SELECT * FROM ssh_tasks WHERE id=?',(task_id,)).fetchone()
        if task:
            message=failure(exc)
            status='unknown' if task['started'] else 'failed'
            # Interrupted running jobs remain recoverable, without automatic replay.
            db.get_conn().execute('UPDATE ssh_tasks SET status=?,error=?,next_check=? WHERE id=?',(status,message,time.time()+60,task_id))
            db.get_conn().execute('UPDATE ssh_servers SET last_error=?,automation_paused=1 WHERE id=?',(message,task['server_id']));db.get_conn().commit()
            try:notify(dict(task),status)
            except Exception:pass
    finally:
        if sftp:sftp.close()
        if t:t.close()
        with LOCK:ACTIVE.discard(task_id)
        if getattr(db._local,'conn',None):db._local.conn.close();db._local.conn=None


def remote_output(sftp,folder):
    output=[]
    for name in ('output.prev','output.log'):
        if exists(sftp,folder+'/'+name):output.append(read(sftp,folder+'/'+name,256*1024))
    return '\n'.join(output)[-131072:]


def q(s):return shlex.quote(str(s))


def tick():
    from . import agent
    sync_hosts();c=db.get_conn();now=time.time()
    # Queue only pending work or confirmed health failure, never continuous SSH polling of healthy hosts.
    with agent.LOCK:
        for s in c.execute("SELECT * FROM ssh_servers WHERE mode='ssh' AND credentials!='' AND automation_paused=0").fetchall():
            if busy(s['id']):continue
            for nid in node_ids(dict(s)):
                a=agent.row(nid)
                if not a:continue
                pending=c.execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND status IN ('queued','running')",(nid,)).fetchone()
                repair=operations.control(nid)['repair_allowed'] and (a['failure_count']>=3 or agent.repair_probe_state(a)['probe_failed'])
                recent=c.execute('SELECT max(created) FROM ssh_tasks WHERE server_id=?',(s['id'],)).fetchone()[0] or 0
                if pending or (repair and now-recent>60):queue(s['id'],'agent_tick',nid);break
    for r in c.execute("SELECT id FROM ssh_tasks WHERE status IN ('queued','running','unknown') AND next_check<=? ORDER BY created LIMIT 10",(now,)).fetchall():
        with LOCK:
            if r['id'] in ACTIVE or len(ACTIVE)>=2:continue
            ACTIVE.add(r['id'])
        threading.Thread(target=work,args=(r['id'],),daemon=True).start()
    c.execute("DELETE FROM ssh_tasks WHERE status IN ('done','failed') AND id NOT IN (SELECT id FROM ssh_tasks ORDER BY created DESC LIMIT 300)");c.commit()


class RelayTarget(deploy.Target):
    relay_node_id:int|None=Field(default=None,ge=1)


class ConnectionBody(RelayTarget):
    name:str=Field(min_length=1,max_length=80)
    host_key:str=Field(min_length=20,max_length=4096)
    password:SecretStr=Field(default=SecretStr(''),max_length=1024)
    private_key:SecretStr=Field(default=SecretStr(''),max_length=20000)
    passphrase:SecretStr=Field(default=SecretStr(''),max_length=1024)


class ActionBody(deploy.BaseModel):
    action:Literal['diagnose','preflight','control_sync','agent_tick','restart','custom','install_key','revoke_key','switch_ssh','switch_agent','bootstrap']
    node_id:int|None=Field(default=None,ge=1)
    script:str=Field(default='',max_length=16000)


class ServerBody(deploy.BaseModel):
    name:str=Field(min_length=1,max_length=80)


class BindingBody(deploy.BaseModel):
    node_ids:list[int]=Field(max_length=100)


def register(app,owner):
    @app.post('/api/ssh/servers')
    def create(body:ServerBody,request:Request):
        owner(request);sid=uuid4().hex
        db.get_conn().execute('INSERT INTO ssh_servers(id,name,created_at) VALUES (?,?,?)',(sid,body.name,db.utcnow()));db.get_conn().commit()
        return public(server(sid))

    @app.get('/api/ssh/servers')
    def listing(request:Request):
        owner(request);sync_hosts();return [public(dict(r)) for r in db.get_conn().execute('SELECT * FROM ssh_servers ORDER BY name')]

    @app.post('/api/ssh/connection-probe')
    def probe(body:RelayTarget,request:Request):
        owner(request)
        t=None
        try:
            t=open_transport({'host':body.host,'port':body.ssh_port,'relay_node_id':body.relay_node_id})
            key=t.get_remote_server_key()
            return {'host_key':key.get_base64(),'fingerprint':deploy.fingerprint(key),'algorithm':key.get_name()}
        except Exception as exc:raise HTTPException(400,failure(exc)) from None
        finally:
            if t:t.close()

    @app.put('/api/ssh/servers/{server_id}')
    def configure(server_id:str,body:ConnectionBody,request:Request):
        owner(request)
        from . import agent
        # Keep network I/O out of the global agent lock; recheck before saving.
        s=server(server_id);original_endpoint=(s['host'],s['port'],s['host_key'],s['relay_node_id'])
        from . import recovery
        if recovery.busy(server_id,include_paused=False):raise HTTPException(409,'恢复流程正在执行，请先暂停或等待完成')
        relay_node(body.relay_node_id,server_id)
        def blocked():
            if db.get_conn().execute("SELECT 1 FROM ssh_tasks WHERE server_id=? AND status IN ('queued','running')",(server_id,)).fetchone():return True
            return busy(server_id) and (body.host,body.ssh_port,body.host_key,body.relay_node_id)!=original_endpoint
        if blocked():raise HTTPException(409,'请先完成当前 SSH 任务；结果未知时可仅更新认证凭据')
        cred={'password':body.password.get_secret_value(),'private_key':body.private_key.get_secret_value(),'passphrase':body.passphrase.get_secret_value()}
        if not cred['password'] and not cred['private_key']:
            if not s['credentials']:raise HTTPException(400,'首次接入请填写 root 密码或私钥')
            cred=unseal(s['credentials'])
        if cred.get('password') and cred.get('private_key'):raise HTTPException(400,'密码与私钥请选择一种')
        s.update(host=body.host,port=body.ssh_port,host_key=body.host_key,relay_node_id=body.relay_node_id)
        t=None
        try:
            t=connect(s,cred)
            channel=paramiko.SFTPClient.from_transport(t);channel.close()
        except Exception as exc:raise HTTPException(400,failure(exc)) from None
        finally:
            if t:t.close()
        with agent.LOCK:
            if blocked():raise HTTPException(409,'已有 SSH 任务开始，请稍后保存')
            kind='managed_key' if s['auth_kind']=='managed_key' and not body.password.get_secret_value() and not body.private_key.get_secret_value() else 'key' if cred.get('private_key') else 'password'
            c=db.get_conn();c.execute('UPDATE ssh_servers SET name=?,host=?,port=?,host_key=?,relay_node_id=?,credentials=?,auth_kind=?,verified_at=?,last_error=\'\' WHERE id=?',
              (body.name,body.host,body.ssh_port,body.host_key,body.relay_node_id,seal(cred),kind,db.utcnow(),server_id));c.commit()
        return public(server(server_id))

    @app.put('/api/ssh/servers/{server_id}/nodes')
    def bind(server_id:str,body:BindingBody,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:
            s=server(server_id)
            from . import recovery
            if recovery.busy(server_id):raise HTTPException(409,'服务器正在恢复，请先完成或取消恢复后再调整关联')
            if busy(server_id) or s['mode']=='ssh':raise HTTPException(409,'请先完成任务并切回主动拉取模式，再调整关联')
            if len(set(body.node_ids))!=len(body.node_ids):raise HTTPException(400,'节点重复')
            if s['relay_node_id'] in body.node_ids:raise HTTPException(409,'中转节点不能关联到当前 SSH 服务器')
            c=db.get_conn()
            for nid in body.node_ids:
                if not agent.row(nid):raise HTTPException(404,'节点不存在')
                other=c.execute('SELECT server_id FROM ssh_nodes WHERE node_id=?',(nid,)).fetchone()
                if other and other['server_id']!=server_id:raise HTTPException(409,'节点已关联其他 SSH 服务器，请先在原服务器解除关联')
            c.execute('DELETE FROM ssh_nodes WHERE server_id=?',(server_id,))
            c.executemany('INSERT INTO ssh_nodes VALUES (?,?)',[(nid,server_id) for nid in body.node_ids]);c.commit()
            return public(server(server_id))

    @app.post('/api/ssh/servers/{server_id}/resume')
    def resume(server_id:str,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:
            s=server(server_id)
            if busy(server_id):raise HTTPException(409,'请先处理未完成或结果未知的任务')
            db.get_conn().execute("UPDATE ssh_servers SET automation_paused=0,last_error='' WHERE id=?",(server_id,));db.get_conn().commit()
            return {'ok':True}

    @app.delete('/api/ssh/servers/{server_id}/credentials')
    def clear_credentials(server_id:str,request:Request):
        owner(request)
        from . import agent
        with agent.LOCK:
            s=server(server_id)
            if busy(server_id) or s['mode']=='ssh':raise HTTPException(409,'请先处理当前任务并切回主动拉取，再清除凭据')
            if s['public_key']:raise HTTPException(409,'请先撤销面板安装的管理密钥')
            db.get_conn().execute("UPDATE ssh_servers SET credentials='',auth_kind='',verified_at='',last_error='',automation_paused=1 WHERE id=?",(server_id,));db.get_conn().commit()
            return {'ok':True}

    @app.get('/api/ssh/servers/{server_id}/tasks')
    def tasks(server_id:str,request:Request):
        owner(request);server(server_id)
        return [dict(r) for r in db.get_conn().execute('SELECT id,node_id,action,status,created,finished,result,error,phase,updated FROM ssh_tasks WHERE server_id=? ORDER BY created DESC LIMIT 30',(server_id,))]

    @app.post('/api/ssh/servers/{server_id}/tasks')
    def start(server_id:str,body:ActionBody,request:Request):
        owner(request)
        from . import agent,upgrades
        with agent.LOCK:
            s=server(server_id)
            from . import recovery
            if recovery.busy(server_id) and body.action not in ('diagnose','preflight'):raise HTTPException(409,'服务器正在恢复，请在恢复详情中处理当前步骤')
            if busy(server_id):raise HTTPException(409,'该服务器有未完成的 SSH 任务')
            if body.node_id and not db.get_conn().execute('SELECT 1 FROM ssh_nodes WHERE server_id=? AND node_id=?',(server_id,body.node_id)).fetchone():raise HTTPException(409,'节点未关联此服务器')
            if body.action=='revoke_key' and s['mode']=='ssh':raise HTTPException(409,'请先切回主动拉取模式，再撤销 SSH 管理密钥')
            if body.action=='revoke_key' and not s['public_key']:raise HTTPException(409,'尚未安装面板专属密钥')
            if body.action in ('restart','agent_tick','bootstrap') and not body.node_id:raise HTTPException(400,'请选择节点')
            if body.action=='custom' and not body.script.strip():raise HTTPException(400,'请输入维护脚本')
            if body.action in ('switch_ssh','switch_agent','restart','custom','install_key','control_sync'):
                if any(upgrades.host_busy(i) for i in node_ids(s)):raise HTTPException(409,'节点有配置或升级任务，请等待完成后再操作')
            if body.action in ('switch_ssh','switch_agent'):
                if not node_ids(s):raise HTTPException(409,'请先关联节点')
                if any(not agent.row(i)['bound'] for i in node_ids(s)):raise HTTPException(409,'请先完成节点安装接入')
                if body.action=='switch_ssh':
                    if any(agent.row(i)['agent_version']<8 for i in node_ids(s)):raise HTTPException(409,'请先将管理脚本升级至 v8，再切换 SSH 管理')
                    # Legacy cron keeps reporting health while SSH executes management work.
            if body.action in ('switch_ssh','switch_agent','restart','custom','control_sync'):
                for nid in node_ids(s):
                    if not operations.maintenance(nid):operations.set_maintenance('node',str(nid),30 if body.action=='custom' else 10,'SSH 维护任务')
            payload={'script':body.script} if body.action=='custom' else {}
            if body.action=='bootstrap':
                a=agent.row(body.node_id)
                if not a or a['bound']:raise HTTPException(409,'此操作仅用于首次安装未绑定节点；已有节点请选择执行节点任务')
                token=agent.enrollment(body.node_id)['command']
                payload={'script':'#!/bin/sh\nset -eu\n'+token+'\n'}
            return queue(server_id,body.action,body.node_id,payload)

    @app.post('/api/ssh/tasks/{task_id}/recheck')
    def recheck(task_id:str,request:Request):
        owner(request)
        c=db.get_conn();r=c.execute('SELECT * FROM ssh_tasks WHERE id=?',(task_id,)).fetchone()
        if not r or r['status'] not in ('unknown','running'):raise HTTPException(409,'该任务无需重新检查')
        c.execute('UPDATE ssh_tasks SET next_check=0 WHERE id=?',(task_id,));c.commit();return {'ok':True}

    @app.post('/api/ssh/tasks/{task_id}/resolve')
    def resolve(task_id:str,request:Request,force:bool=False):
        owner(request)
        r=db.get_conn().execute("SELECT * FROM ssh_tasks WHERE id=? AND status='unknown'",(task_id,)).fetchone()
        if not r:raise HTTPException(409,'仅可处理结果未知的任务')
        with LOCK:
            if task_id in ACTIVE:raise HTTPException(409,'正在检查此任务，请稍后操作')
            ACTIVE.add(task_id)
        t=None
        try:
            if force:
                finish(dict(r),'failed',error='管理员确认服务器已重置并放弃旧任务；未执行远端清理，自动运维保持暂停')
                return {'ok':True,'remote_cleanup_confirmed':False}
            t=connect(server(r['server_id']));folder=REMOTE+'/'+task_id
            check='for f in /proc/[0-9]*/environ; do if tr "\\000" "\\n" < "$f" 2>/dev/null | grep -Fxq '+q('ARGO_SSH_TASK_ID='+task_id)+'; then exit 0; fi; done; exit 1'
            code,_=command(t,check)
            if code==0:raise HTTPException(409,'远端任务仍在运行，继续等待或通过 SSH 人工处理')
            finish(dict(r),'failed',error='管理员确认远端进程已结束，结果仍未知；请核对实际状态后再恢复自动运维')
            return {'ok':True}
        except HTTPException:raise
        except Exception as exc:raise HTTPException(400,failure(exc)) from None
        finally:
            if t:t.close()
            with LOCK:ACTIVE.discard(task_id)
