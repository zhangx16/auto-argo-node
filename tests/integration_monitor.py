"""Exercise a real patched Monitor hub + Rust probe through the panel WS gateway.
Run only against the disposable hub configured by MONITOR_HUB_URL.
"""
import base64,hashlib,json,os,shutil,socket,sqlite3,subprocess,time,urllib.request
from pathlib import Path
from cryptography.fernet import Fernet
from test_api import ApiTests

ApiTests.setUpClass()
client=ApiTests();probe=core=None;profile=None
try:
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    tmp=Path(client.tmp.name);binary=tmp/'sing-box';shutil.copy('/usr/local/bin/sing-box-probe',binary);binary.chmod(0o755)
    config=tmp/'core.json';config.write_text(json.dumps({'inbounds':[{'type':'socks','listen':'127.0.0.1','listen_port':port}],'outbounds':[{'type':'direct'}]}))
    core=subprocess.Popen([str(binary),'run','-c',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    code,n=client.call('/api/agent-nodes','POST',{'remark':'Monitor isolated QA','node_port':port,'client_address':'127.0.0.1','client_port':port,'protocol':'reality'})
    assert code==200,(code,n)
    node_id=n['id'];c=sqlite3.connect(tmp/'app.db');c.row_factory=sqlite3.Row
    c.execute("UPDATE node_agents SET bound=1,applied_revision=1,health_mode='none' WHERE node_id=?",(node_id,));c.execute("UPDATE agent_jobs SET status='done' WHERE node_id=?",(node_id,));c.commit()
    code,d=client.call(f'/api/monitor/nodes/{node_id}/enrollment','POST',{});assert code==200,(code,d)
    h=c.execute('SELECT * FROM monitor_hosts').fetchone()
    secret=(tmp/'secret.key').read_bytes().strip();cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'argo-cloudflare-v1:'+secret).digest()))
    token=cipher.decrypt(h['token_secret'].encode()).decode()
    manifests=Path('/var/lib/argo-monitor/services');manifests.mkdir(parents=True,exist_ok=True)
    profile=manifests/f'{node_id}.json';profile.write_text(json.dumps({'node_id':node_id,'revision':1,'port':port,'protocol':'reality','published_at':int(time.time())}))
    probe=subprocess.Popen(['/releases/monitor-agent-amd64','--interval','2'],env=os.environ|{'MONITOR_SERVER':client.base+'/monitor','MONITOR_TOKEN':token},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for _ in range(80):
        code,data=client.call('/api/monitor/hosts');host=data['hosts'][0]
        if host['online'] and host['health'].get('services'):break
        time.sleep(.25)
    assert host['online'] and host['health']['services'][0]['local_listen'],host
    req=urllib.request.Request(client.base+'/monitor/activate',method='POST',headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(req) as r:assert node_id in json.load(r)['nodes']
    assert client.call('/api/monitor/hosts',authed=False)[0]==401
    assert client.call(f"/api/monitor/hosts/{h['id']}/history")[0]==200
    # The node token must not be an administrator token.
    req=urllib.request.Request(os.environ['MONITOR_HUB_URL']+'/api/nodes',headers={'X-Argo-Service':token})
    try:urllib.request.urlopen(req);raise AssertionError('agent became admin')
    except urllib.error.HTTPError as e:assert e.code==401
    print('PASS real Rust probe: resource samples, service binding, activation, private APIs, history; observing gateway keepalive',flush=True)
    time.sleep(135)
    code,data=client.call('/api/monitor/hosts');host=data['hosts'][0]
    assert host['online'] and not host['stale'],host
    assert probe.poll() is None
    core.terminate();core.wait();core=None
    for _ in range(80):
        state=c.execute('SELECT health_status FROM node_agents WHERE node_id=?',(node_id,)).fetchone()[0]
        if state=='degraded':break
        time.sleep(.5)
    assert state=='degraded',state
    print('PASS >120s gateway keepalive and stopped-core health detection',flush=True)
finally:
    for p in (probe,core):
        if p and p.poll() is None:p.terminate();p.wait(timeout=10)
    if profile:profile.unlink(missing_ok=True)
    ApiTests.tearDownClass()
