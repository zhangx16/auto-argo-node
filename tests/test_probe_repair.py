import json,os,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from app import agent,db,node_probe
import test_reliability

class ProbeRepairTests(unittest.TestCase):
    setUp=test_reliability.ReliabilityTests.setUp
    tearDown=test_reliability.ReliabilityTests.tearDown
    def run_probe(self,effects):
        with patch.object(node_probe,'probe_link',side_effect=effects) as probe,patch.object(agent.STOP,'wait',return_value=False):
            self.assertTrue(self.real_schedule(self.id))
            for _ in range(200):
                if self.id not in agent.PROBING:break
                time.sleep(.01)
            self.assertNotIn(self.id,agent.PROBING)
            return probe.call_count
    def test_transient_failure_retries_before_marking_down(self):
        self.assertEqual(self.run_probe([ValueError('transient'),{'latency_ms':10}]),2)
        a=agent.row(self.id);self.assertEqual(a['probe_status'],'ok');self.assertEqual(a['probe_failures'],0)
    def test_only_consecutive_confirmed_probe_failures_authorize_repair(self):
        for count in (1,2):
            self.assertEqual(self.run_probe([ValueError('failure')]*2),2)
            a=agent.row(self.id);self.assertEqual(a['probe_failures'],count)
            self.assertEqual(agent.repair_probe_state(a)['probe_failed'],count>=2)
        self.run_probe([{'latency_ms':8}]);self.assertFalse(agent.repair_probe_state(agent.row(self.id))['probe_failed'])
        self.assertFalse(agent.repair_probe_state(agent.row(self.id)|{'probe_status':'failed','probe_failures':3,'probe_at':'2020-01-01T00:00:00Z'})['probe_failed'])
    def test_native_probe_fallback_requires_actual_https_trace(self):
        process=MagicMock();process.poll.return_value=None
        for fallback,ok in [(b'ip=192.0.2.1\nh=www.cloudflare.com\n\n200',True),(b'<html>error</html>\n200',False),(b'ip=192.0.2.1\n403',False)]:
            calls=[subprocess.CompletedProcess([],28,b'000'),subprocess.CompletedProcess([],0,fallback)]
            with patch('app.node_formats.parse_uri',return_value={}),patch('app.node_formats.singbox_outbound',return_value={}),patch('subprocess.Popen',return_value=process),patch('socket.create_connection',return_value=MagicMock()),patch('subprocess.run',side_effect=calls) as run:
                if ok:self.assertEqual(node_probe.native_probe('fixture')['http_status'],200)
                else:
                    with self.assertRaises(ValueError):node_probe.native_probe('fixture')
                self.assertEqual(run.call_count,2)
    def test_real_shell_does_not_repair_same_failed_probe_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'base';node=root/'node';bin=root/'bin'
            for p in (base,node,bin):p.mkdir()
            node.joinpath('settings.json').write_text(json.dumps({'panel':'https://fixture.invalid','node_id':35,'port':33001,'protocol':'reality','health_mode':'monitor','management_enabled':True,'applied_revision':1}))
            node.joinpath('direct.json').write_text('{}');node.joinpath('curl.conf').write_text('')
            control=root/'control';control.write_text(json.dumps({'job':None,'health_mode':'monitor','health':{'probe_failed':True,'probe_at':'2026-10-23T15:00:00Z','failure_count':0,'health_status':'online','process_alive':True,'local_ws':True}}))
            base.joinpath('repair.sh').write_text('printf "repair\\n" >> "$1/attempts"\n')
            fake='''#!/usr/bin/env python3
import sys,json,os
from pathlib import Path
a=sys.argv;out=Path(a[a.index('-o')+1]);url=a[-1]
if url.endswith('/agent/repair-permit'):out.write_text(json.dumps({'allowed':json.loads(Path(os.environ['MOCK_CONTROL']).read_text()).get('permit',True)}))
elif url.endswith('/agent/work'):out.write_text(Path(os.environ['MOCK_CONTROL']).read_text())
else:out.write_text('{"applied_revision":1}')
print('200',end='')
'''
            bin.joinpath('curl').write_text(fake);bin.joinpath('pgrep').write_text('#!/bin/sh\nexit 0\n');bin.joinpath('ss').write_text('#!/bin/sh\necho "LISTEN 0 100 *:33001 0.0.0.0:*"\n')
            for p in bin.iterdir():p.chmod(0o755)
            script=(Path(agent.__file__).parent/'node-agent.sh').read_text().replace('BASE=/usr/local/lib/argo-deploy','BASE='+str(base))
            script=script.replace('case "$DIR" in /usr/local/etc/argo-deploy/node-[0-9]*) ;; *) exit 1;; esac','case "$DIR" in '+str(node)+') ;; *) exit 1;; esac')
            script=script.replace('export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','export PATH='+str(bin)+':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
            file=root/'agent.sh';file.write_text(script)
            env=os.environ|{'MOCK_CONTROL':str(control)}
            def run():
                p=subprocess.run(['sh',str(file),str(node)],capture_output=True,text=True,env=env,timeout=15)
                self.assertEqual(p.returncode,0,p.stderr)
            run();self.assertEqual(node.joinpath('attempts').read_text().count('repair'),1)
            node.joinpath('last-repair').write_text('0');run()
            self.assertEqual(node.joinpath('attempts').read_text().count('repair'),1)
            current=json.loads(control.read_text());current['health']['probe_at']='2026-10-23T15:05:00Z';control.write_text(json.dumps(current));node.joinpath('last-repair').write_text('0');run()
            self.assertEqual(node.joinpath('attempts').read_text().count('repair'),2)
            current['health']['probe_at']='2026-10-23T15:10:00Z';current['permit']=False
            control.write_text(json.dumps(current));node.joinpath('last-repair').write_text('0');run()
            self.assertEqual(node.joinpath('attempts').read_text().count('repair'),2)
            current['permit']=True;current['control']={'repair_allowed':False}
            control.write_text(json.dumps(current));run()
            self.assertEqual(node.joinpath('attempts').read_text().count('repair'),2)
