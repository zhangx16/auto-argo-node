import hashlib
import json
import re
import subprocess
import time
import unittest
from types import SimpleNamespace
from datetime import datetime,timezone
from unittest.mock import MagicMock,patch
from fastapi import FastAPI,HTTPException
from app import db,agent,monitor,servers,recovery,ssh_control as ssh,subscriptions,subscription_health as health,controlplane,task_center,schema,capabilities
import test_reliability
import test_ssh_control


class ServerWorkflows(unittest.TestCase):
    setUp=test_reliability.ReliabilityTests.setUp
    tearDown=test_reliability.ReliabilityTests.tearDown
    host=test_ssh_control.SSHControlTests.host

    def ready(self):
        self.sid=self.host();subscriptions.init();self.req=SimpleNamespace(headers={})
        self.addCleanup(patch.stopall)
        patch.object(agent,'PUBLIC_URL',lambda:'https://panel.example.com').start()
        patch.object(monitor,'PUBLIC_URL',lambda:'https://panel.example.com').start()
        db.get_conn().execute("UPDATE node_agents SET agent_version=8,health_mode='none' WHERE node_id=?",(self.id,));db.get_conn().commit()
        app=FastAPI();agent.register(app,lambda request:'owner',lambda:'https://panel.example.com')
        self.routes={r.path:r.endpoint for r in app.routes}
        return self.sid

    def test_versioned_additive_migrations_preserve_values_and_repair_legacy_drift(self):
        self.ready();c=db.get_conn();c.execute('UPDATE ssh_servers SET notes=? WHERE id=?',('keep',self.sid));c.commit()
        before=c.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]
        agent.init();self.assertEqual(ssh.server(self.sid)['notes'],'keep')
        self.assertEqual(c.execute('SELECT count(*) FROM schema_migrations').fetchone()[0],before)
        self.assertEqual(schema.status()['busy_timeout_ms'],10000)
        self.assertEqual(schema.status()['journal_mode'],'wal')

    def test_control_origins_are_explicit_and_command_falls_back_without_changing_scope(self):
        self.ready();c=db.get_conn();c.execute('UPDATE ssh_servers SET control_url=?,fallback_urls=? WHERE id=?',('https://control.example.com',json.dumps(['https://backup.example.com']),self.sid));c.commit()
        result=agent.enrollment(self.id);subprocess.run(['sh','-n'],input=result['command'].encode(),check=True)
        self.assertIn('backup.example.com',result['command'])
        token=re.search(r'/agent/bootstrap/([A-Za-z0-9_-]+)\.sh',result['command']).group(1)
        script=self.routes['/agent/bootstrap/{token}.sh'](token,'https://backup.example.com').body.decode()
        self.assertIn('PANEL=https://backup.example.com',script)
        with self.assertRaises(HTTPException):self.routes['/agent/bootstrap/{token}.sh'](token,'https://untrusted.example.com')
        for value in ('https://user:password@example.com','https://example.com/path','https://example.com?x=1','file:///etc/passwd'):
            with self.assertRaises(HTTPException):controlplane.clean(value)
        self.assertEqual(controlplane.origins('https://panel.example.com',{'control_url':'','fallback_urls':'["https://backup.example.com"]'}),['https://panel.example.com','https://backup.example.com'])

    def test_inventory_joins_components_and_never_returns_secrets(self):
        self.ready();data=servers.describe(ssh.server(self.sid))
        self.assertEqual(data['nodes'][0]['id'],self.id);self.assertEqual(data['nodes'][0]['protocol'],'vless-ws-argo')
        self.assertTrue(data['nodes'][0]['display_name'].endswith('-cf'))
        self.assertEqual(data['protocols'][0]['count'],1)
        text=json.dumps(data)
        for forbidden in ('private-fixture-password','token_hash','desired_json','credentials','private_key'):self.assertNotIn(forbidden,text)

    def test_multi_node_creation_inherits_server_and_checks_port_conflicts(self):
        self.ready();endpoint=self.routes['/api/servers/{server_id}/nodes']
        with self.assertRaises(HTTPException):endpoint(self.sid,servers.NewNode(name='conflict',node_port=18080,tunnel_mode='temp',auto_install=False),self.req)
        original=db.get_node(self.id)['share_link']
        result=endpoint(self.sid,servers.NewNode(name='Second',node_port=18081,tunnel_mode='temp',auto_install=False),self.req)
        new=result['id'];self.assertEqual(ssh.binding(new)['id'],self.sid)
        self.assertEqual(db.get_node(self.id)['share_link'],original)
        self.assertNotEqual(db.get_node(new)['uuid'],db.get_node(self.id)['uuid'])
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM ssh_servers').fetchone()[0],1)

    def test_new_node_auto_install_reuses_probe_and_does_not_run_on_create_thread(self):
        self.ready();result=self.routes['/api/servers/{server_id}/nodes'](self.sid,servers.NewNode(name='Second',node_port=18081,tunnel_mode='temp'),self.req)
        self.assertEqual(result['recovery']['status'],'running')
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM ssh_tasks').fetchone()[0],0)
        self.assertFalse(result['reuses_probe']);self.assertFalse(agent.row(result['id'])['bound'])

    def test_recovery_requires_explicit_confirmation_and_blocks_other_host_mutations(self):
        self.ready()
        with self.assertRaises(HTTPException):recovery.create(self.sid,recovery.Start(node_ids=[self.id]))
        result=recovery.create(self.sid,recovery.Start(node_ids=[self.id],confirmed=True,install_probe=False))
        with self.assertRaises(HTTPException):agent.enqueue(self.id,'read',self.config)
        with self.assertRaises(HTTPException):recovery.create(self.sid,recovery.Start(node_ids=[self.id],confirmed=True))
        self.assertTrue(ssh.server(self.sid)['automation_paused'])
        self.assertEqual(result['position'],0)

    def test_durable_recovery_advances_only_after_real_job_and_forwarding_success(self):
        self.ready();run=recovery.create(self.sid,recovery.Start(node_ids=[self.id],confirmed=True,install_probe=False));rid=run['id'];c=db.get_conn()
        recovery.tick();first=json.loads(recovery.get(rid)['steps'])[0]['task_id'];recovery.tick()
        self.assertEqual(c.execute('SELECT count(*) FROM ssh_tasks').fetchone()[0],1)
        c.execute("UPDATE ssh_servers SET capabilities=? WHERE id=?",(json.dumps({'install_supported':True}),self.sid))
        c.execute("UPDATE ssh_tasks SET status='done' WHERE id=?",(first,));c.commit();recovery.tick();recovery.tick()
        step=json.loads(recovery.get(rid)['steps'])[1];task=dict(c.execute('SELECT * FROM ssh_tasks WHERE id=?',(step['task_id'],)).fetchone())
        token=re.search(r'/agent/bootstrap/([A-Za-z0-9_-]+)\.sh',ssh.unseal(task['payload'])['script']).group(1)
        reply=self.routes['/agent/enroll'](agent.EnrollBody(enrollment_token=token,hostname='restored',os='alpine',arch='x86_64'))
        self.assertEqual(reply['node_id'],self.id)
        rev=c.execute('SELECT expected_revision FROM recovery_nodes WHERE run_id=?',(rid,)).fetchone()[0]
        self.assertEqual(c.execute('SELECT operation FROM agent_jobs WHERE node_id=? AND revision=?',(self.id,rev)).fetchone()[0],'install')
        c.execute("UPDATE ssh_tasks SET status='done' WHERE id=?",(task['id'],));c.commit();recovery.tick()
        self.assertEqual(recovery.get(rid)['position'],1,'SSH exit alone does not prove protocol install')
        c.execute("UPDATE agent_jobs SET status='done' WHERE node_id=? AND revision=?",(self.id,rev))
        c.execute('UPDATE node_agents SET applied_revision=? WHERE node_id=?',(rev,self.id));c.commit();recovery.tick()
        with patch.object(agent,'schedule_probe',return_value=True):recovery.tick()
        c.execute("UPDATE node_agents SET probe_status='ok',probe_at='2020-01-01T00:00:00Z' WHERE node_id=?",(self.id,));c.commit()
        with patch.object(agent,'schedule_probe',return_value=True):recovery.tick()
        self.assertEqual(recovery.get(rid)['position'],2,'old successful probe cannot pass restoration')
        c.execute('UPDATE node_agents SET probe_at=?,last_report_at=? WHERE node_id=?',(db.utcnow(),db.utcnow(),self.id));c.commit();recovery.tick();recovery.tick();recovery.tick()
        self.assertEqual(recovery.get(rid)['status'],'done');self.assertFalse(ssh.server(self.sid)['automation_paused'])
        self.assertEqual(db.get_node(self.id)['share_link'],self.link)

    def test_unknown_recovery_task_pauses_and_cannot_be_replayed_by_resume(self):
        self.ready();run=recovery.create(self.sid,recovery.Start(node_ids=[self.id],confirmed=True,install_probe=False));recovery.tick()
        tid=json.loads(recovery.get(run['id'])['steps'])[0]['task_id']
        db.get_conn().execute("UPDATE ssh_tasks SET status='unknown',started=1 WHERE id=?",(tid,));db.get_conn().commit();recovery.tick()
        self.assertEqual(recovery.get(run['id'])['status'],'paused')
        with self.assertRaises(HTTPException):self.routes['/api/recoveries/{run_id}/resume'](run['id'],self.req)
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM ssh_tasks').fetchone()[0],1)

    def test_task_inventory_normalizes_sources_and_omits_payload(self):
        self.ready();ssh.queue(self.sid,'diagnose');data=task_center.listing()
        self.assertEqual({r['kind'] for r in data['items']},{'node','ssh'})
        self.assertNotIn('payload',json.dumps(data));self.assertNotIn('private-fixture-password',json.dumps(data))
        self.assertEqual(task_center.error_info('SSH 主机指纹已变化','failed')['code'],'host_identity')

    def test_server_tasks_are_isolated_and_include_its_upgrade_batches(self):
        self.ready();from app import upgrades
        c=db.get_conn();ssh.queue(self.sid,'diagnose')
        c.execute("INSERT INTO upgrade_batches(kind,status,created_at,fingerprint) VALUES ('agent','paused',?,'fixture')",(db.utcnow(),));bid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("INSERT INTO upgrade_items(batch_id,position,node_id,name) VALUES (?,0,?,'fixture')",(bid,self.id));c.commit()
        items=task_center.listing(server_id=self.sid)['items']
        self.assertEqual({x['kind'] for x in items},{'ssh','node','upgrade'})
        self.assertTrue(all(x['server_id']==self.sid for x in items))
        self.assertEqual(task_center.listing(server_id='other-server')['items'],[])
        app=FastAPI();task_center.register(app,lambda request:'owner');output=next(r.endpoint for r in app.routes if r.path=='/api/tasks/{task_id}/output')
        result=output('upgrade:'+str(bid),self.req,self.sid);self.assertIn('fixture',result['result'])
        with self.assertRaises(HTTPException):output('upgrade:'+str(bid),self.req,'other-server')

    def test_server_traffic_totals_and_history_exclude_other_hosts(self):
        self.ready();from app import telemetry
        c=db.get_conn();other=db.create_node('Other server',19001,'www.amd.com','','fixture-other')['id'];day=db.utcnow()[:10]
        c.execute('INSERT INTO traffic_daily(node_id,day,rx,tx) VALUES (?,?,11,22)',(self.id,day))
        c.execute('INSERT INTO traffic_daily(node_id,day,rx,tx) VALUES (?,?,1000,2000)',(other,day));c.commit()
        result=telemetry.summary(server_id=self.sid)
        self.assertEqual([n['id'] for n in result['nodes']],[self.id]);self.assertEqual(result['totals']['today_rx'],11)
        self.assertEqual(result['history']['points'][-1]['rx'],11)
        with self.assertRaises(HTTPException):telemetry.summary(node_id=other,server_id=self.sid)
        with self.assertRaises(HTTPException):telemetry.summary(server_id='missing')

    def test_preflight_caches_structured_bounded_capabilities(self):
        self.ready();report={'os':'alpine','arch':'x86_64','init':'openrc','memory_kib':128*1024,'disk_free_kib':1024*1024,'container':'lxc','public_ipv6':False}
        with patch.object(ssh,'command',return_value=(0,json.dumps(report))):data=capabilities.inspect(MagicMock(),self.sid)
        self.assertTrue(data['install_supported']);self.assertTrue(data['warnings'])
        self.assertFalse(capabilities.cached(ssh.server(self.sid))['stale'])
        with patch.object(ssh,'command',return_value=(0,json.dumps(report|{'init':'unknown'}))):data=capabilities.inspect(MagicMock(),self.sid)
        with self.assertRaises(HTTPException):capabilities.require_install(data)

    def test_recovery_explicitly_replaces_only_selected_queued_work(self):
        self.ready();revision=agent.enqueue(self.id,'read',self.config)
        with self.assertRaises(HTTPException):recovery.create(self.sid,recovery.Start(node_ids=[self.id],confirmed=True,install_probe=False))
        run=recovery.create(self.sid,recovery.Start(node_ids=[self.id],confirmed=True,install_probe=False,replace_queued=True))
        self.assertEqual(run['status'],'running')
        self.assertEqual(db.get_conn().execute('SELECT status FROM agent_jobs WHERE node_id=? AND revision=?',(self.id,revision)).fetchone()[0],'failed')

    def test_paused_forwarding_can_fix_client_port_without_reinstalling_protocol(self):
        self.ready();run=recovery.create(self.sid,recovery.Start(node_ids=[self.id],confirmed=True,install_probe=False));rid=run['id'];c=db.get_conn()
        steps=run['steps'];steps[0]['status']='done';steps[1]['status']='done';steps[2]['status']='running'
        c.execute("UPDATE recovery_runs SET status='paused',position=2,steps=? WHERE id=?",(json.dumps(steps),rid))
        c.execute('UPDATE node_agents SET desired_revision=2,applied_revision=2 WHERE node_id=?',(self.id,))
        c.execute("INSERT INTO agent_jobs(node_id,revision,operation,config_json,status,created_at) VALUES (?,2,'install',?,'done',?)",(self.id,json.dumps(self.config),db.utcnow()));c.commit()
        count=c.execute('SELECT count(*) FROM agent_jobs').fetchone()[0]
        with patch.object(agent,'schedule_probe',return_value=True):
            result=self.routes['/api/recoveries/{run_id}/connection'](rid,recovery.ConnectionFix(client_port=8443),self.req)
        self.assertEqual(result['status'],'paused')
        self.assertIn(':8443?',db.get_node(self.id)['share_link'])
        self.assertEqual(c.execute('SELECT count(*) FROM agent_jobs').fetchone()[0],count)
        self.assertEqual(agent.row(self.id)['actual_port'],18080)
        stored=c.execute('SELECT config FROM recovery_nodes WHERE run_id=?',(rid,)).fetchone()[0]
        self.assertEqual(json.loads(stored),json.loads(agent.row(self.id)['desired_json']))
        c.execute("UPDATE recovery_runs SET status='running' WHERE id=?",(rid,));c.commit()
        with self.assertRaises(HTTPException):self.routes['/api/recoveries/{run_id}/connection'](rid,recovery.ConnectionFix(client_port=443),self.req)

    def test_recovery_probe_gets_priority_over_periodic_probes(self):
        self.ready();calls=[]
        db.get_conn().execute("UPDATE node_agents SET probe_at='2020-01-01T00:00:00Z' WHERE node_id=?",(self.id,));db.get_conn().commit()
        with patch.object(recovery,'tick',side_effect=lambda:calls.append('recovery')),patch.object(agent,'schedule_probe',side_effect=lambda nid:calls.append('periodic')):
            agent.probe_cycle()
        self.assertEqual(calls,['recovery','periodic'])

    def test_waiting_for_probe_capacity_does_not_count_as_forwarding_failure(self):
        self.ready();run=recovery.create(self.sid,recovery.Start(node_ids=[self.id],confirmed=True,install_probe=False));rid=run['id'];c=db.get_conn();steps=run['steps']
        steps[2].update(started=time.time()-200,status='running')
        c.execute('UPDATE recovery_runs SET position=2,steps=? WHERE id=?',(json.dumps(steps),rid))
        c.execute("UPDATE node_agents SET probe_at='2020-01-01T00:00:00Z' WHERE node_id=?",(self.id,));c.commit()
        with patch.object(agent,'schedule_probe',return_value=False):recovery.tick()
        self.assertEqual(recovery.get(rid)['status'],'running');self.assertEqual(recovery.get(rid)['error'],'')

    def test_relay_runtime_is_shared_only_within_nested_connection_lifetime(self):
        from app import proxy_runtime
        runtime=MagicMock();runtime.__enter__.return_value=19000
        with patch.object(proxy_runtime,'runtime',return_value=runtime) as factory:
            with ssh.relay_runtime('fixture-link') as first:
                with ssh.relay_runtime('fixture-link') as second:self.assertEqual(first,second)
                runtime.__exit__.assert_not_called()
            factory.assert_called_once();runtime.__exit__.assert_called_once()

    def test_panel_client_budget_defers_without_allocating_another_slot(self):
        from app import runtime_budget
        from contextlib import ExitStack
        with ExitStack() as stack:
            for _ in range(4):stack.enter_context(runtime_budget.slot(timeout=0))
            self.assertEqual(runtime_budget.status()['active'],4)
            with self.assertRaises(runtime_budget.Deferred):
                with runtime_budget.slot(timeout=0):pass
        self.assertEqual(runtime_budget.status()['active'],0)


class SubscriptionIsolation(unittest.TestCase):
    host=ServerWorkflows.host
    ready=ServerWorkflows.ready
    tearDown=ServerWorkflows.tearDown
    def setUp(self):
        test_reliability.ReliabilityTests.setUp(self);self.ready();self.sub=subscriptions.save(subscriptions.SubscriptionBody(name='Isolated',node_ids=[self.id]))
        db.get_conn().execute('INSERT INTO subscription_health_policy VALUES (?,1,2,1,2,1)',(self.sub['id'],));db.get_conn().commit()
        self.base=time.time()

    def sample(self,state,offset):
        at=datetime.fromtimestamp(self.base+offset,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        c=db.get_conn();c.execute('UPDATE node_agents SET probe_status=?,probe_at=? WHERE node_id=?',(state,at,self.id));c.commit()
        health.observe(self.id,state,at,agent.row(self.id)['applied_revision'],db.get_node(self.id)['share_link'],now=self.base+offset)

    def test_quarantine_and_recovery_require_count_and_dwell_and_preserve_membership(self):
        self.sample('failed',0);self.assertFalse(health.isolated_ids(self.sub['id']))
        self.sample('failed',30);self.assertFalse(health.isolated_ids(self.sub['id']))
        health.tick(self.base+61);self.assertEqual(health.isolated_ids(self.sub['id']),{self.id})
        self.assertEqual(len(subscriptions.member_nodes(self.sub['id'])),1)
        with self.assertRaises(HTTPException):subscriptions.render(self.sub,'raw')
        self.sample('ok',70);self.sample('ok',100);self.assertTrue(health.isolated_ids(self.sub['id']))
        health.tick(self.base+131);self.assertFalse(health.isolated_ids(self.sub['id']))
        self.assertIn('vless://',subscriptions.render(self.sub,'raw')[0])

    def test_duplicate_samples_stale_and_maintenance_do_not_trigger_isolation(self):
        self.sample('failed',0);self.sample('failed',0)
        r=db.get_conn().execute('SELECT failures FROM subscription_health_nodes').fetchone();self.assertEqual(r[0],1)
        health.tick(self.base+1000);self.assertFalse(health.isolated_ids(self.sub['id']))
        from app import operations
        operations.set_maintenance('node',str(self.id),30,'fixture')
        self.sample('failed',30);self.sample('failed',100);self.assertFalse(health.isolated_ids(self.sub['id']))

    def test_policy_disabled_has_no_effect_and_isolation_is_per_subscription(self):
        other=subscriptions.save(subscriptions.SubscriptionBody(name='Other',node_ids=[self.id]))
        self.sample('failed',0);self.sample('failed',61)
        self.assertTrue(health.isolated_ids(self.sub['id']));self.assertFalse(health.isolated_ids(other['id']))
        self.assertIn('vless://',subscriptions.render(other,'raw')[0])
        db.get_conn().execute('UPDATE subscription_health_policy SET enabled=0');db.get_conn().commit()
        self.assertIn('vless://',subscriptions.render(self.sub,'raw')[0])

    def test_new_config_does_not_inherit_old_failure_streak(self):
        self.sample('failed',0)
        db.get_conn().execute('UPDATE node_agents SET applied_revision=2 WHERE node_id=?',(self.id,));db.get_conn().commit()
        self.sample('failed',61)
        r=db.get_conn().execute('SELECT failures FROM subscription_health_nodes').fetchone();self.assertEqual(r[0],1)
        self.assertFalse(health.isolated_ids(self.sub['id']))

    def test_chain_isolation_never_bypasses_required_hops(self):
        second=db.create_node('Hop2',18082,'hop.example.com','','hop-token')['id'];db.update_node(second,status='online',share_link=self.link)
        from app.proxy_chains import Chain
        chain=subscriptions.save(subscriptions.SubscriptionBody(name='Chain',node_ids=[self.id,second],chain_only=True,chains=[Chain(name='Route',hops=[{'kind':'managed','id':self.id},{'kind':'managed','id':second}])]))
        db.get_conn().execute('INSERT INTO subscription_health_policy VALUES (?,1,2,1,2,1)',(chain['id'],));db.get_conn().commit()
        self.sample('failed',0);self.sample('failed',61)
        with self.assertRaises(HTTPException) as error:subscriptions.render(chain,'singbox')
        self.assertIn('不会自动绕过',error.exception.detail)
        self.assertEqual(subscriptions.get(chain['id'])['chains_json'],json.dumps([{'name':'Route','hops':[{'kind':'managed','id':self.id},{'kind':'managed','id':second}]}]))
