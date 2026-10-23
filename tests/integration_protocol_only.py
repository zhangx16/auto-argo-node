"""Three real protocol-only installations in disposable Alpine/OpenRC containers."""
import json,subprocess,time
PANEL='argo-install-panel';NODE='argo-install-node'
CODE='''import json,sys,urllib.request,urllib.error,base64
from pathlib import Path
from itsdangerous import TimestampSigner
from app import db
p=json.load(sys.stdin);secret=Path('/app/data/secret.key').read_text().strip()
cookie=TimestampSigner(secret).sign(base64.b64encode(json.dumps({'user':db.get_setting('admin_user'),'authed':True}).encode())).decode()
r=urllib.request.Request('http://127.0.0.1:18899'+p['path'],method=p['method'],headers={'Cookie':'sbmgmt='+cookie,'Content-Type':'application/json'},data=json.dumps(p['body']).encode() if p['body'] is not None else None)
with urllib.request.urlopen(r,timeout=90) as response:print(json.dumps(json.load(response)))
'''
def api(path,method='GET',body=None):
 r=subprocess.run(['docker','exec','-i',PANEL,'python','-c',CODE],input=json.dumps(dict(path=path,method=method,body=body)).encode(),capture_output=True,check=True);return json.loads(r.stdout)
def node(cmd):
 r=subprocess.run(['docker','exec','-i',NODE,'sh'],input=cmd.encode(),capture_output=True,timeout=900)
 if r.returncode:raise RuntimeError('isolated node command failed: '+r.stderr.decode()[-500:])
 return r.stdout.decode()
for _ in range(100):
 if subprocess.run(['docker','exec',NODE,'test','-f','/run/openrc/softlevel'],capture_output=True).returncode==0 and subprocess.run(['docker','exec',PANEL,'curl','-fsS','http://127.0.0.1:18899/health'],capture_output=True).returncode==0:break
 time.sleep(.2)
ip=subprocess.check_output(['docker','inspect','-f','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',NODE]).decode().strip()
ids=[]
for kind,port in [('reality',18443),('hysteria2',18444),('vless-ws-argo',18445)]:
 body=dict(remark='Isolated '+kind,protocol=kind,node_port=port,client_address=ip if kind!='vless-ws-argo' else '',client_port=port if kind!='vless-ws-argo' else 443,core_channel='minimal',install_monitor=False,management_enabled=False)
 result=api('/api/agent-nodes','POST',body);ids.append(result['id'])
 node(result['command']+'\n')
 current=next(n for n in api('/api/nodes') if n['id']==result['id'])
 assert current['share_link'] and current['job_status']=='done',(kind,current['last_error'])
 assert current['health_mode']=='none' and not current['management_enabled']
 cron=node('crontab -l 2>/dev/null || true\n');assert '# argo-deploy-node-' not in cron and '# argo-monitor-publish' not in cron
 api(f"/api/agent-nodes/{result['id']}/probe",'POST')
 for _ in range(65):
  current=next(n for n in api('/api/nodes') if n['id']==result['id'])
  if current['probe_status'] in ('ok','failed'):break
  time.sleep(1)
 if kind=='vless-ws-argo':
  # Quick Tunnel DNS may still be cached negative by the panel resolver.
  for _ in range(9):
   if current['probe_status']=='ok':break
   time.sleep(10);api(f"/api/agent-nodes/{result['id']}/probe",'POST');time.sleep(3)
   current=next(n for n in api('/api/nodes') if n['id']==result['id'])
 assert current['probe_status']=='ok',(kind,current['probe_error'])
 print('PASS protocol-only installation + real forwarding + no management/resource cron:',kind,flush=True)
# Rollback exercised independently in production pilot and unit tests; verify node removal remains explicit.
for id in reversed(ids):
 response=api(f'/api/nodes/{id}','DELETE')
 assert response['manual_command']
 node(response['manual_command']+'\n')
 assert all(n['id']!=id for n in api('/api/nodes'))
print('PASS manual uninstall of all three protocols, clean isolated panel',flush=True)
