import subprocess,json
import os
C=os.environ.get('TRAFFIC_QA_CONTAINER','argo-telemetry-node')
def run(code):
 r=subprocess.run(['docker','exec','-i',C,'sh'],input=code.encode(),capture_output=True,check=True);return r.stdout.decode()
subprocess.run(['docker','cp',str(__import__('pathlib').Path(__file__).resolve().parent.parent/'app/node-traffic.sh'),C+':/tmp/traffic.sh'],check=True,capture_output=True)
setup='''set -eu
DIR=/tmp/traffic-fixture;WORK=/tmp/traffic-work;mkdir -p "$DIR" "$WORK"
NODE_ID=999;PORT=21122;PROTOCOL=reality
. /tmp/traffic.sh
traffic_sample
cat "$WORK/traffic.json"
'''
a=json.loads(run(setup));assert a['rx']==0 and a['tx']==0
run('nohup nc -l -p 21122 > /tmp/traffic-payload </dev/null &\n')
run('head -c 8192 /dev/zero | timeout 2 nc 127.0.0.1 21122 || true\n')
b=json.loads(run(setup));assert b['epoch']==a['epoch'] and b['rx']>8192 and b['tx']>0
run('nft delete table inet argo_traffic_999\n')
c=json.loads(run(setup));assert c['epoch']!=b['epoch']
print('PASS real kernel TCP bytes and counter-generation recovery')
u=setup.replace('PORT=21122;PROTOCOL=reality','PORT=21123;PROTOCOL=hysteria2')
a=json.loads(run(u));run('nohup nc -u -l -p 21123 > /tmp/traffic-udp-payload </dev/null &\n')
run('head -c 2048 /dev/zero | timeout 2 nc -u 127.0.0.1 21123 || true\n')
b=json.loads(run(u));assert b['rx']>=2048
print('PASS real kernel UDP bytes; no traffic-control rules')
run('nft delete table inet argo_traffic_999; rm -rf /tmp/traffic-fixture /tmp/traffic-work\n')
