import shutil
import json
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from app import resources,db,agent,agent_scripts,fixed_jobs
import test_reliability as rt

SAMPLE=dict(sampled_at=100,cpu_percent=12.5,cpu_cores=2,load1=.5,memory_total=1024,memory_used=512,swap_total=0,swap_used=0,disk_total=2048,disk_used=1024,uptime_seconds=1000,singbox_rss=128,cloudflared_rss=64,singbox_processes=1,cloudflared_processes=1)

class ResourceTests(unittest.TestCase):
    setUp=rt.ReliabilityTests.setUp
    tearDown=rt.ReliabilityTests.tearDown
    def test_retention_bucket_updates_and_stale_receipt_time(self):
        s=resources.Sample(**SAMPLE)
        with patch.object(resources.time,'time',return_value=90000):resources.record(self.id,s);db.get_conn().commit()
        with patch.object(resources.time,'time',return_value=90060):
            resources.record(self.id,s);db.get_conn().commit()
            data=resources.snapshot()['nodes'][0]
            self.assertFalse(data['stale']);self.assertEqual(data['sample']['sampled_at'],100)
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM node_resource_history').fetchone()[0],1)
        with patch.object(resources.time,'time',return_value=180000):resources.record(self.id,s);db.get_conn().commit()
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM node_resource_history').fetchone()[0],1)
        with patch.object(resources.time,'time',return_value=180181):self.assertTrue(resources.snapshot()['nodes'][0]['stale'])
        encoded=json.dumps(data)
        self.assertNotIn(rt.UUID,encoded);self.assertNotIn('token',encoded);self.assertNotIn('desired_json',encoded)
        db.delete_node(self.id)
        for table in ('node_resource_latest','node_resource_history'):
            self.assertEqual(db.get_conn().execute(f'SELECT count(*) FROM {table}').fetchone()[0],0)
    def test_optional_old_reports_validation_and_first_sample(self):
        self.assertIsNone(agent.Report(state='online').resources)
        for field,val in [('cpu_percent',101),('cpu_percent',float('nan')),('load1',float('inf')),('memory_used',-1)]:
            with self.assertRaises(ValidationError):resources.Sample(**dict(SAMPLE,**{field:val}))
        self.assertIsNone(resources.Sample(**dict(SAMPLE,cpu_percent=None)).cpu_percent)
        resources.record(self.id,None)
        self.assertIsNone(resources.snapshot()['nodes'][0]['sample'])
    def test_accept_report_persists_resources_with_health(self):
        agent.accept_report(agent.row(self.id),agent.Report(state='online',agent_version=5,resources=resources.Sample(**SAMPLE),metadata=agent.Metadata(domain='old.trycloudflare.com',uuid=rt.UUID,path='/ws',local_port=18080),process_alive=True,local_ws=True,public_ws=True))
        self.assertEqual(resources.snapshot()['nodes'][0]['sample']['cpu_percent'],12.5)
        self.assertEqual(agent.row(self.id)['agent_version'],5)
    @unittest.skipUnless(shutil.which("jq"), "shell collector requires the node jq dependency")
    def test_shell_collector_validates_and_no_persistent_worker(self):
        with tempfile.TemporaryDirectory() as d:
            code='DIR="$1";WORK="$1";. "$2";resource_sample;cat "$WORK/resources.json"'
            path=str(Path(agent.__file__).with_name('node-resources.sh'))
            result=subprocess.run(['sh','-c',code,'sh',d,path],capture_output=True,text=True,check=True)
            sample=resources.Sample(**json.loads(result.stdout));self.assertIsNone(sample.cpu_percent);self.assertGreater(sample.memory_total,0)
            result=subprocess.run(['sh','-c',code,'sh',d,path],capture_output=True,text=True,check=True)
            resources.Sample(**json.loads(result.stdout))
            self.assertNotIn('curl',Path(path).read_text());self.assertNotIn('sleep',Path(path).read_text().split('resource_sample()')[1])
    def test_install_and_upgrade_include_new_assets_and_diagnostics_redact(self):
        for script in [agent_scripts.bootstrap_script('https://example.com','fixture',1),fixed_jobs.upgrade(1,18080)]:
            self.assertIn('resources.sh diagnose.sh',script)
            subprocess.run(['sh','-n'],input=script,text=True,check=True)
        diag=Path(agent.__file__).with_name('node-diagnose.sh').read_text()
        self.assertNotIn('cmdline',diag.split('set -u')[1]);self.assertNotIn('curl.conf',diag)
        self.assertNotIn('cat "$DIR/settings.json"',diag)

class ResourceHttpTests(unittest.TestCase):
    import test_subscriptions as st
    setUpClass=classmethod(st.SubscriptionTests.setUpClass.__func__)
    tearDownClass=classmethod(st.SubscriptionTests.tearDownClass.__func__)
    call=st.SubscriptionTests.call
    create_agent=st.SubscriptionTests.create_agent
    enroll_agent=st.SubscriptionTests.enroll_agent
    agent_call=st.SubscriptionTests.agent_call
    initial_success=st.SubscriptionTests.initial_success
    def test_owner_only_resources_and_scoped_report(self):
        for path in ('/api/resources','/api/resources/1/history'):
            self.assertEqual(self.call(path,authed=False)[0],401)
        node_id,token,meta=self.initial_success()
        code,result=self.agent_call('/agent/report',token,dict(state='online',metadata=meta,resources=SAMPLE,agent_version=5,process_alive=True,local_ws=True,public_ws=True))
        self.assertEqual(code,200,result)
        code,result=self.call('/api/resources');self.assertEqual(code,200)
        node=next(n for n in result['nodes'] if n['node_id']==node_id)
        self.assertEqual(node['sample']['memory_used'],512);self.assertNotIn(token,json.dumps(result))
        code,result=self.call(f'/api/resources/{node_id}/history');self.assertEqual(code,200);self.assertEqual(len(result['samples']),1)
        self.assertEqual(self.call('/api/resources/999999/history')[0],404)
        for name in ['resources.sh','diagnose.sh']:
            import urllib.request
            with urllib.request.urlopen(self.base+'/agent/assets/'+name) as response:
                self.assertEqual(response.status,200)
                subprocess.run(['sh','-n'],input=response.read(),check=True)
