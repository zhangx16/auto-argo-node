"""Run with disposable sb-agent-panel / sb-agent-node containers; never production data."""
import json
import subprocess
import sys
import time
from urllib.parse import urlparse

PANEL='sb-agent-panel';NODE='sb-agent-node'
API_CODE='''import json,sys,urllib.request,urllib.error,base64
from pathlib import Path
from itsdangerous import TimestampSigner
from app import db
p=json.load(sys.stdin);secret=Path('/app/data/secret.key').read_text().strip()
cookie=TimestampSigner(secret).sign(base64.b64encode(json.dumps({'user':db.get_setting('admin_user'),'authed':True}).encode())).decode()
r=urllib.request.Request('http://sb-agent-panel:18899'+p['path'],method=p.get('method','GET'),headers={'Cookie':'sbmgmt='+cookie,'Content-Type':'application/json'},data=None if 'body' not in p else json.dumps(p['body']).encode())
try:
 with urllib.request.urlopen(r,timeout=40) as response:print(json.dumps({'code':response.status,'data':json.load(response)}))
except urllib.error.HTTPError as e:print(json.dumps({'code':e.code,'data':json.load(e)}))
'''

def api(path,method='GET',body=None):
    data={'path':path,'method':method}
    if body is not None:data['body']=body
    r=subprocess.run(['docker','exec','-i',PANEL,'python','-c',API_CODE],input=json.dumps(data).encode(),capture_output=True,check=True)
    response=json.loads(r.stdout)
    if response['code']>=400:raise RuntimeError(str(response))
    return response['data']

def node_command(command,capture=False):
    r=subprocess.run(['docker','exec','-i',NODE,'sh'],input=command.encode(),capture_output=capture,check=True)
    return r.stdout.decode() if capture else ''

def tick(id):
    node_command(f'sh /usr/local/lib/argo-deploy/agent.sh /usr/local/etc/argo-deploy/node-{id}\n')

def get(id):return next(x for x in api('/api/nodes') if x['id']==id)

for _ in range(100):
    panel_ready=subprocess.run(['docker','exec',PANEL,'curl','-fsS','http://127.0.0.1:18899/health'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    node_ready=subprocess.run(['docker','exec',NODE,'test','-f','/run/openrc/softlevel'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if panel_ready and node_ready:break
    time.sleep(.2)
else:raise RuntimeError('QA containers did not become ready')
print('Creating isolated command-based node',flush=True)
result=api('/api/agent-nodes','POST',{'remark':'Agent integration QA','node_port':18180,'client_address':'','client_port':443})
id=result['id'];node_command(result['command']+'\n')
n=get(id)
if not n['share_link']:
    for l in api(f'/api/nodes/{id}/logs'):print(l['line'])
assert n['share_link'] and n['node_port']==18180,n.get('last_error')
print('PASS: command enrollment, installation, heartbeat and link callback',flush=True)
for _ in range(35):
    n=get(id)
    if n['probe_status'] in ('ok','failed'):break
    time.sleep(1)
assert n['probe_status']=='ok',n['probe_error']
print('PASS: panel-side full VLESS + HTTPS probe',flush=True)
node_command('rc-service dcron stop\n')
old_domain=n['sni']
# STOP keeps the process alive while breaking its ability to carry tunnel traffic.
node_command("kill -STOP \"$(cat /run/singboxlite/argo-18180.pid)\"\n")
for i in range(3):tick(id)
n=get(id)
assert n['repair_count']>=1,n
assert n['sni']!=old_domain,n
if not n['public_ws']:
    # A published Quick Tunnel hostname may need a further round before traffic reaches it.
    tick(id);n=get(id)
assert n['public_ws'] and n['local_ws'],n
print('PASS: alive-but-unresponsive tunnel detected, rebuilt and synced to panel',flush=True)
repairs=n['repair_count'];current_domain=n['sni']
node_command("kill -STOP \"$(cat /run/singboxlite/argo-18180.pid)\"\n")
for i in range(3):tick(id)
n=get(id);assert n['repair_count']==repairs and n['sni']==current_domain
print('PASS: five-minute repair cooldown prevents restart loop',flush=True)
node_command("kill -CONT \"$(cat /run/singboxlite/argo-18180.pid)\"\n");tick(id)
api(f'/api/agent-nodes/{id}','PATCH',{'remark':'Agent edited','node_port':18182,'client_address':'','client_port':443})
assert get(id)['node_port']==18180
# Run exactly the same one-shot cron task that normal installations run each minute.
tick(id);n=get(id);assert n['node_port']==18182 and n['desired_revision']==n['applied_revision'],n['last_error']
print('PASS: queued port edit applied and acknowledged on original record',flush=True)
for _ in range(35):
    n=get(id)
    if n['probe_status'] in ('ok','failed'):break
    time.sleep(1)
assert n['probe_status']=='ok',n['probe_error']
command=api(f'/api/agent-nodes/{id}/enrollment','POST');node_command(command['command']+'\n')
node_command('rc-service dcron stop\n')
count=node_command("jq '.inbounds | length' /usr/local/etc/sing-box/config.json\n",capture=True).strip();assert count=='1',count
print('PASS: re-enrollment keeps existing node without duplicate inbounds',flush=True)
# No monitoring daemon remains resident between ticks (cron itself is intentionally reused).
processes=node_command("ps -o args | grep '[a]gent.sh' || true\n",capture=True)
assert not processes.strip(),processes
print('PASS: no resident agent.sh process between scheduled runs',flush=True)
api(f'/api/nodes/{id}','DELETE');tick(id)
assert not any(n['id']==id for n in api('/api/nodes'))
node_command('test ! -e /usr/local/etc/sing-box/config.json && test ! -e /usr/local/etc/argo-deploy && test ! -e /usr/local/lib/argo-deploy\n')
print('PASS: deleting node uninstalls remote configuration, monitoring scripts and service',flush=True)
print('All command-agent integration checks passed.',flush=True)
