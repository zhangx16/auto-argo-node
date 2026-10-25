import json,subprocess,unittest
from pathlib import Path
from app import agent,db
import test_reliability

class HeartbeatRestorationTests(unittest.TestCase):
    setUp=test_reliability.ReliabilityTests.setUp
    tearDown=test_reliability.ReliabilityTests.tearDown
    def test_restore_and_remove_jobs_keep_protocol_identity_and_validate_shell(self):
        a=agent.row(self.id);cfg=json.loads(a['desired_json']);before=db.get_node(self.id)['share_link']
        for op,phase in [('legacy_restore','restore'),('monitor_remove','remove')]:
            script=agent.job_script(a,{'operation':op,'config_json':json.dumps(cfg),'revision':2})
            subprocess.run(['sh','-n'],input=script.encode(),check=True)
            self.assertIn('restore-heartbeat.sh',script);self.assertIn('sh "$TMP/restore.sh" '+phase,script)
            self.assertLess(script.index('sh "$TMP/restore.sh" '+phase),script.index("printf 'SB_WEB_RESULT='"))
        self.assertEqual(db.get_node(self.id)['share_link'],before)
    def test_restore_script_requires_legacy_before_uninstall_and_preserves_protocols(self):
        script=(agent.ROOT/'node-restore-heartbeat.sh').read_text();subprocess.run(['sh','-n'],input=script.encode(),check=True)
        self.assertIn('.health_mode=="legacy"',script);self.assertIn('crontab "$TMP/cron"',script)
        self.assertIn('/agent/restore-heartbeat',script);self.assertNotIn('rm -rf /usr/local/etc/sing-box',script)
        self.assertLess(script.index('.health_mode=="legacy"'),script.index('systemctl disable --now argo-monitor'))
    def test_legacy_report_updates_health_without_probe(self):
        c=db.get_conn();c.execute("UPDATE node_agents SET health_mode='legacy',health_status='degraded',last_report_at='2020-01-01T00:00:00Z' WHERE node_id=?",(self.id,));c.commit()
        agent.accept_report(agent.row(self.id),agent.Report(state='online',local_ws=True,public_ws=True,process_alive=True))
        a=agent.row(self.id);self.assertEqual(a['health_status'],'online');self.assertGreater(a['last_report_at'],'2020-01-01T00:00:00Z')
