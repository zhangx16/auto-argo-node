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
    def test_heartbeat_cron_is_installed_even_when_mutation_is_disabled(self):
        script=agent_scripts.bootstrap_script('https://panel.example.com','fixture-enrollment',self.id,{'management_enabled':0})
        self.assertIn("printf '* * * * * sh",script)
        self.assertIn('apk add --no-cache nftables',script)
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

    def test_host_traffic_modes_unlimited_and_exceeded(self):
        base={'month_rx':70,'month_tx':30,'traffic_limit':80,'traffic_reset_day':20,'month_start':'2026-10-20'}
        for mode,used in [('sum',100),('up',30),('down',70),('max',70)]:
            q=monitor.host_traffic(base|{'traffic_mode':mode})
            self.assertEqual(q['used_bytes'],used);self.assertEqual(q['percent'],round(used/80*100,2))
            self.assertEqual(q['remaining_bytes'],max(0,80-used));self.assertEqual(q['exceeded'],used>=80)
            self.assertEqual(q['reset_day'],20);self.assertEqual(q['cycle_start'],'2026-10-20')
        q=monitor.host_traffic(base|{'traffic_limit':0});self.assertIsNone(q['percent']);self.assertIsNone(q['remaining_bytes'])
        self.assertFalse(q['exceeded']);self.assertIsNone(monitor.host_traffic(None))

    def test_host_traffic_write_scoped_to_mapped_hub_host(self):
        self.host()
        body=monitor.HostTrafficRule(traffic_limit=1024**4,traffic_mode='up',traffic_reset_day=15)
        with patch.object(monitor,'hub',return_value={'ok':True}) as hub:
            self.assertEqual(monitor.set_host_traffic('fixture',body),{'ok':True})
            hub.assert_called_once_with('/nodes/1','PUT',{'traffic_limit':1024**4,'traffic_mode':'up','traffic_reset_day':15})
        from fastapi import HTTPException
        with patch.object(monitor,'hub') as hub:
            with self.assertRaises(HTTPException):monitor.set_host_traffic('missing',body)
            hub.assert_not_called()
        for invalid in [{'traffic_limit':-1},{'traffic_mode':'invalid'},{'traffic_reset_day':0},{'traffic_reset_day':32},{'traffic_limit':1.5},{'traffic_limit':2**61},{'name':'unexpected'},{'traffic_reset_day':True}]:
            with self.assertRaises(ValidationError):monitor.HostTrafficRule(**invalid)

    def test_host_classification_validation_and_persistence(self):
        self.host()
        self.assertEqual(monitor.set_host_group('fixture',monitor.HostGroup(group_name='  亚洲 / 家宽  ')),{'group_name':'亚洲 / 家宽'})
        self.assertEqual(monitor.get_host('fixture')['group_name'],'亚洲 / 家宽')
        monitor.set_host_group('fixture',monitor.HostGroup());self.assertEqual(monitor.get_host('fixture')['group_name'],'')
        for v in ['x'*41,'bad\nname','bad\x00name']:
            with self.assertRaises(ValidationError):monitor.HostGroup(group_name=v)
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):monitor.set_host_group('missing',monitor.HostGroup(group_name='QA'))

    def test_filtered_order_preserves_other_positions_and_rejects_stale_ids(self):
        c=db.get_conn()
        for i in range(4):c.execute('INSERT INTO monitor_hosts(id,name,hub_id,hub_token,token_hash,token_secret,sort_order) VALUES (?,?,?,?,?,?,?)',(f'host-{i}',f'Host {i}',i+1,'unused',f'hash-{i}','unused',i))
        c.commit()
        self.assertEqual(monitor.reorder_hosts(monitor.HostOrder(host_ids=['host-3','host-1']))['host_ids'],['host-0','host-3','host-2','host-1'])
        self.assertEqual(monitor.ordered_host_ids(),['host-0','host-3','host-2','host-1'])
        self.assertEqual(monitor.reorder_hosts(monitor.HostOrder(host_ids=['host-2','host-1','host-3','host-0']))['host_ids'],['host-2','host-1','host-3','host-0'])
        from fastapi import HTTPException
        before=monitor.ordered_host_ids()
        with self.assertRaises(HTTPException):monitor.reorder_hosts(monitor.HostOrder(host_ids=['host-0','deleted-host']))
        self.assertEqual(monitor.ordered_host_ids(),before)
        for v in [[],['host-0','host-0'],[''],['x'*65]]:
            with self.assertRaises(ValidationError):monitor.HostOrder(host_ids=v)

    def test_monitor_schema_upgrade_keeps_existing_host_and_is_idempotent(self):
        self.host();c=db.get_conn();before=monitor.get_host('fixture')
        # Simulate the pre-sort production schema.
        c.execute('ALTER TABLE monitor_hosts DROP COLUMN group_name');c.execute('ALTER TABLE monitor_hosts DROP COLUMN sort_order');c.commit()
        monitor.init();monitor.init();after=monitor.get_host('fixture')
        self.assertEqual(after['token_secret'],before['token_secret']);self.assertEqual(after['group_name'],'');self.assertEqual(after['sort_order'],0)
