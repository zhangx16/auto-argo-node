import json,time,unittest,subprocess
from unittest.mock import patch
from pydantic import ValidationError
from app import agent,db,monitor,agent_scripts
import test_reliability as rt

class MonitorContracts(unittest.TestCase):
    setUp=rt.ReliabilityTests.setUp
    tearDown=rt.ReliabilityTests.tearDown
    def host(self):
        c=db.get_conn();c.execute("INSERT INTO monitor_hosts(id,name,hub_id,hub_token,token_hash,token_secret) VALUES ('fixture','QA',1,'encrypted','hash','encrypted')")
        c.execute('INSERT INTO monitor_nodes VALUES (?,?)',(self.id,'fixture'));c.commit()
    def payload(self,**overrides):
        s=dict(node_id=self.id,revision=1,port=18080,protocol='vless-ws-argo',process_alive=True,local_listen=True,published_at=int(time.time()))|overrides
        return dict(version=1,sampled_at=int(time.time()),singbox_rss=1024,cloudflared_rss=512,singbox_processes=1,cloudflared_processes=1,services=[s])
    def test_staged_probe_cannot_replace_health_before_activation(self):
        self.host();before=agent.row(self.id)['last_report_at']
        monitor.observe('fixture',self.payload(local_listen=False))
        self.assertEqual(agent.row(self.id)['health_status'],'online')
        self.assertEqual(agent.row(self.id)['last_report_at'],before)
        h=monitor.get_host('fixture');self.assertEqual(json.loads(h['health'])['services'][0]['node_id'],self.id)
    def test_probe_rejects_wrong_binding_or_revision_and_keeps_node_identity(self):
        self.host();c=db.get_conn();c.execute("UPDATE node_agents SET health_mode='monitor' WHERE node_id=?",(self.id,));c.commit()
        monitor.observe('fixture',self.payload(revision=99,local_listen=False))
        self.assertEqual(agent.row(self.id)['health_status'],'online')
        monitor.observe('fixture',self.payload(node_id=self.id+10,local_listen=False))
        self.assertEqual(agent.row(self.id)['health_status'],'online')
        monitor.observe('fixture',self.payload(local_listen=False))
        self.assertEqual(agent.row(self.id)['health_status'],'degraded')
        self.assertEqual(db.get_node(self.id)['share_link'],self.link)
    def test_management_callback_does_not_reset_probe_failure_or_freshness(self):
        self.host();c=db.get_conn();c.execute("UPDATE node_agents SET health_mode='monitor',last_report_at='2020-01-01T00:00:00Z',health_status='degraded',failure_count=4 WHERE node_id=?",(self.id,));c.commit()
        agent.accept_report(agent.row(self.id),agent.Report(state='online',local_ws=True,process_alive=True,public_ws=True))
        a=agent.row(self.id);self.assertEqual(a['failure_count'],4);self.assertEqual(a['last_report_at'],'2020-01-01T00:00:00Z');self.assertEqual(a['health_status'],'degraded')
    def test_protocol_only_has_no_cron_and_old_bootstrap_still_managed(self):
        script=agent_scripts.bootstrap_script('https://panel.example.com','fixture-enrollment',self.id,{'management_enabled':0})
        self.assertNotIn("printf '* * * * * sh",script)
        self.assertNotIn('apk add --no-cache nftables',script)
        self.assertIn('sh "$BASE/agent.sh" "$DIR"',script)
        for s in [script,agent_scripts.bootstrap_script('https://panel.example.com','fixture',self.id)]:subprocess.run(['sh','-n'],input=s.encode(),check=True)
    def test_unmonitored_is_not_offline_and_deletion_keeps_host(self):
        self.host();c=db.get_conn();c.execute("UPDATE node_agents SET health_mode='none',last_report_at='' WHERE node_id=?",(self.id,));c.commit()
        n=agent.enrich(dict(db.get_node(self.id)));self.assertEqual(n['health_status'],'unmonitored');self.assertNotEqual(n['status'],'offline')
        db.delete_node(self.id);self.assertEqual(db.get_conn().execute('SELECT count(*) FROM monitor_hosts').fetchone()[0],1)
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM monitor_nodes').fetchone()[0],0)
    def test_report_limits(self):
        for changes in [dict(singbox_rss=-1),dict(services=[self.payload()['services'][0]]*129)]:
            with self.assertRaises(ValidationError):monitor.ServiceReport.model_validate(self.payload()|changes)
