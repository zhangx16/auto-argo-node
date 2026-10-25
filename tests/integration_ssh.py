"""Run only against the disposable SSH peer in tests/Dockerfile.ssh."""
import json,os,secrets,tempfile,time,socket,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app import db,agent,deploy,ssh_control as ssh

host=os.environ['SSH_TEST_HOST'];password=os.environ['SSH_TEST_PASSWORD']
with tempfile.TemporaryDirectory() as directory:
    db.DATA_DIR=Path(directory);db.DB_PATH=db.DATA_DIR/'app.db';db._local.conn=None
    (db.DATA_DIR/'secret.key').write_text(secrets.token_hex(32))
    db.init_db();deploy.init();agent.init()
    pinned=deploy.probe(deploy.Target(host=host))['host_key'];sid='fixture-ssh'
    def conn():return db.get_conn()
    conn().execute('INSERT INTO ssh_servers(id,name,host,host_key,credentials,auth_kind,created_at) VALUES (?,?,?,?,?,?,?)',
        (sid,'SSH fixture',host,pinned,ssh.seal({'password':password}),'password',db.utcnow()));conn().commit()
    def remote(command):
        t=ssh.connect(ssh.server(sid));code,out=ssh.command(t,command);t.close();assert code==0,out;return out
    def drain(task_id):
        for _ in range(50):
            ssh.work(task_id)
            r=dict(conn().execute('SELECT * FROM ssh_tasks WHERE id=?',(task_id,)).fetchone())
            if r['status'] in ('done','failed'):return r
            time.sleep(.1)
        raise AssertionError('task did not finish: '+r['error'])
    assert remote('id -u').strip()=='0'
    try:ssh.connect(ssh.server(sid)|{'host_key':'wrong-host-key'})
    except ssh.HostChanged:pass
    else:raise AssertionError('unpinned host accepted')
    r=drain(ssh.queue(sid,'diagnose')['id']);assert r['status']=='done',r['error'];assert 'root uid: 0' in r['result']
    r=drain(ssh.queue(sid,'preflight')['id']);assert r['status']=='done',r['error']
    data=json.loads(r['result']);assert data['memory_kib']>0 and data['arch'] in ('x86_64','aarch64')
    live=ssh.queue(sid,'custom',payload={'script':'echo ARGO_STAGE=protocol_install; echo fixture-live-progress; sleep 3; echo fixture-live-finished'})
    ssh.work(live['id']);saw_live=False
    for _ in range(30):
        time.sleep(.1);ssh.work(live['id']);r=dict(conn().execute('SELECT * FROM ssh_tasks WHERE id=?',(live['id'],)).fetchone())
        if r['status']=='running' and 'fixture-live-progress' in r['result']:
            saw_live=True;assert r['phase']=='protocol_install';break
    assert saw_live,'output was buffered until task completion'
    r=drain(live['id']);assert r['status']=='done' and 'fixture-live-finished' in r['result']
    task=ssh.queue(sid,'custom',payload={'script':"printf 'once\\n' >> /tmp/fixture-executions; sleep 1; echo fixture-complete"})
    original=ssh.command
    def disconnect_after_launch(t,command,timeout=20):
        result=original(t,command,timeout)
        if command.startswith('nohup '):raise ssh.TimeoutErrorSSH()
        return result
    with patch.object(ssh,'command',side_effect=disconnect_after_launch):ssh.work(task['id'])
    assert conn().execute('SELECT status FROM ssh_tasks WHERE id=?',(task['id'],)).fetchone()[0]=='unknown'
    r=drain(task['id']);assert r['status']=='done',r['error'];assert remote('cat /tmp/fixture-executions').splitlines()==['once']
    # A missing journal after launch is never treated as permission to replay.
    task=ssh.queue(sid,'custom',payload={'script':"echo duplicate >> /tmp/fixture-executions"})
    conn().execute('UPDATE ssh_tasks SET started=1 WHERE id=?',(task['id'],));conn().commit();ssh.work(task['id'])
    assert conn().execute('SELECT status FROM ssh_tasks WHERE id=?',(task['id'],)).fetchone()[0]=='unknown'
    assert remote('cat /tmp/fixture-executions').splitlines()==['once']
    conn().execute("UPDATE ssh_tasks SET status='failed' WHERE id=?",(task['id'],));conn().commit()
    # Seed one owned node profile and unrelated cron entry.
    n=db.create_node('SSH node',18080,'fixture.invalid','fixture.invalid','fixture-enrollment');nid=n['id'];token='fixture-runtime-token'
    conn().execute('INSERT INTO node_agents(node_id,bound,token_hash,desired_json,actual_port) VALUES (?,1,?,?,18080)',(nid,agent.digest(token),json.dumps({'node_port':18080,'uuid':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','ws_path':'/ws'})))
    conn().execute('INSERT INTO ssh_nodes VALUES (?,?)',(nid,sid));conn().commit()
    t=ssh.connect(ssh.server(sid));sftp=paramiko.SFTPClient.from_transport(t)
    code,_=ssh.command(t,f'mkdir -p /usr/local/etc/argo-deploy/node-{nid} /usr/local/lib/argo-deploy');assert code==0
    ssh.write(sftp,f'/usr/local/etc/argo-deploy/node-{nid}/curl.conf',f'header = "X-Agent-Token: {token}"\n')
    ssh.write(sftp,f'/usr/local/etc/argo-deploy/node-{nid}/settings.json',json.dumps({'management_enabled':True,'port':18080}))
    ssh.write(sftp,'/tmp/fixture-cron',f'5 * * * * echo untouched # unrelated\n* * * * * echo fixture # argo-deploy-node-{nid}\n')
    ssh.command(t,'crontab /tmp/fixture-cron');sftp.close();t.close()
    r=drain(ssh.queue(sid,'switch_ssh')['id']);assert r['status']=='done',r['result']
    assert ssh.controls(nid);cron=remote('crontab -l');assert 'unrelated' in cron and 'argo-deploy-node-' not in cron
    r=drain(ssh.queue(sid,'switch_agent')['id']);assert r['status']=='done',r['result']
    assert not ssh.controls(nid);cron=remote('crontab -l');assert 'unrelated' in cron and f'argo-deploy-node-{nid}' in cron
    # Run the existing one-shot worker over SSH and check that only this execution carries the SSH marker.
    headers=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.reply()
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')));self.reply()
        def reply(self):
            if self.path!='/health':headers.append(self.headers.get('X-SSH-Execution'))
            body={'job':None,'health_mode':'none','control':{'repair_allowed':False},'health':{}} if self.path=='/agent/work' else {'applied_revision':1}
            data=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    http=ThreadingHTTPServer(('0.0.0.0',0),Handler);threading.Thread(target=http.serve_forever,daemon=True).start()
    t=ssh.connect(ssh.server(sid));sftp=paramiko.SFTPClient.from_transport(t)
    settings={'node_id':nid,'port':18080,'panel':'http://'+socket.gethostbyname(socket.gethostname())+':'+str(http.server_port),'protocol':'reality','health_mode':'none','management_enabled':True,'applied_revision':1}
    agent.PUBLIC_URL=lambda:settings['panel']
    ssh.write(sftp,f'/usr/local/etc/argo-deploy/node-{nid}/settings.json',json.dumps(settings))
    ssh.write(sftp,f'/usr/local/etc/argo-deploy/node-{nid}/direct.json',json.dumps({'type':'direct','protocol':'reality','uuid':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','local_port':18080,'domain':'fixture.invalid','path':'/'}))
    ssh.write(sftp,'/usr/local/lib/argo-deploy/agent.sh',Path('/app/app/node-agent.sh').read_text(),0o700)
    sftp.close();t.close()
    conn().execute('UPDATE ssh_servers SET control_url=? WHERE id=?',(settings['panel'],sid));conn().commit()
    r=drain(ssh.queue(sid,'control_sync')['id']);assert r['status']=='done',(r['error'],r['result'])
    assert json.loads(remote(f'cat /usr/local/etc/argo-deploy/node-{nid}/settings.json'))['panel']==settings['panel']
    r=drain(ssh.queue(sid,'agent_tick',nid)['id']);assert r['status']=='done',r['result'];assert len(headers)>=3 and all(v=='1' for v in headers),headers
    http.shutdown();http.server_close()
    r=drain(ssh.queue(sid,'custom',payload={'script':'dd if=/dev/zero of=/tmp/ssh-large-fixture bs=1024 count=512; seq 1 40000; echo bounded-output-complete'})['id'])
    assert r['status']=='done',r['error'];assert 'bounded-output-complete' in r['result'];assert len(r['result'])<=24000
    assert remote('wc -c < /tmp/ssh-large-fixture').strip()=='524288'
    # Independent key creation and rotation preserve unrelated authorized keys.
    other=Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()+' fixture-unrelated'
    t=ssh.connect(ssh.server(sid));sftp=paramiko.SFTPClient.from_transport(t)
    ssh.command(t,'mkdir -p /root/.ssh; chmod 700 /root/.ssh');ssh.write(sftp,'/root/.ssh/authorized_keys',other+'\n');sftp.close();t.close()
    original_connect=ssh.connect
    def reject_new_key(server,credentials=None):
        if credentials is not None:raise paramiko.AuthenticationException()
        return original_connect(server)
    task=ssh.queue(sid,'install_key')
    with patch.object(ssh,'connect',side_effect=reject_new_key):ssh.work(task['id'])
    assert ssh.unseal(ssh.server(sid)['credentials'])['password']==password
    assert remote('cat /root/.ssh/authorized_keys').strip()==other
    r=drain(ssh.queue(sid,'install_key')['id']);assert r['status']=='done',r['error']
    first=ssh.server(sid)['public_key'];assert 'password' not in ssh.unseal(ssh.server(sid)['credentials'])
    assert remote('id -u').strip()=='0';assert other in remote('cat /root/.ssh/authorized_keys')
    r=drain(ssh.queue(sid,'install_key')['id']);assert r['status']=='done',r['error']
    assert first not in remote('cat /root/.ssh/authorized_keys');assert other in remote('cat /root/.ssh/authorized_keys')
    r=drain(ssh.queue(sid,'revoke_key')['id']);assert r['status']=='done',r['error']
    assert not ssh.public(ssh.server(sid))['configured']
    t=ssh.connect(ssh.server(sid),{'password':password});_,out=ssh.command(t,'cat /root/.ssh/authorized_keys');t.close();assert out.strip()==other
    assert password not in json.dumps(ssh.public(ssh.server(sid)))
    print('SSH integration passed: host pinning, reconnect without replay, missing journal, cron handoff, key validation/rotation/revocation, unrelated keys preserved.')
