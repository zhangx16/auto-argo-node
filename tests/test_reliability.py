import json
from datetime import date
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from pydantic import ValidationError
from app import db,agent,deploy,events,quotas,changes,telemetry,node_probe
import test_subscriptions as st

UUID='341cad6d-9bf5-4b09-9c96-a7879ebc8fe1'

class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=getattr(db._local,'conn',None)
        self.patches=[patch.object(db,'DB_PATH',Path(self.tmp.name)/'app.db'),patch.object(db,'DATA_DIR',Path(self.tmp.name))]
        for p in self.patches:p.start()
        db._local.conn=None;Path(self.tmp.name,'secret.key').write_text('fixture-reliability-secret')
        db.init_db();deploy.init();agent.init();agent.STOP.clear()
        self.id=db.create_node('Reliability QA',18080,'old.trycloudflare.com','old.example.com','fixture-token')['id']
        self.config={'uuid':UUID,'node_port':18080,'ws_path':'/ws'}
        body=agent.AgentEditBody(remark='Reliability QA',node_port=18080,client_address='old.example.com',client_port=443)
        self.link=deploy.make_link(dict(domain='old.trycloudflare.com',uuid=UUID,path='/ws'),body)
        db.update_node(self.id,status='online',uuid=UUID,share_link=self.link)
        c=db.get_conn();c.execute('INSERT INTO deployments VALUES (?,?,?,?,?,?)',(self.id,'',22,443,'/ws',''))
        c.execute("INSERT INTO node_agents(node_id,bound,desired_revision,applied_revision,desired_json,actual_port,probe_status,last_report_at,health_status) VALUES (?,1,1,1,?,18080,'ok',?,'online')",(self.id,json.dumps(self.config),db.utcnow()))
        c.execute('UPDATE node_agents SET probe_at=? WHERE node_id=?',(db.utcnow(),self.id))
        c.execute("INSERT INTO agent_jobs(node_id,revision,operation,config_json,created_at,status) VALUES (?,1,'install',?,?,'done')",(self.id,json.dumps(self.config),db.utcnow()));c.commit()
        self.real_schedule=agent.schedule_probe
        self.probe_patch=patch.object(agent,'schedule_probe',return_value=False);self.probe_patch.start()
    def tearDown(self):
        for _ in range(100):
            if self.id not in agent.PROBING:break
            time.sleep(.05)
        self.probe_patch.stop()
        db.get_conn().close();db._local.conn=self.old
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def edit(self,port=18080,address='new.example.com',client_port=443):
        return agent.edit(self.id,agent.AgentEditBody(remark='Changed',node_port=port,client_address=address,client_port=client_port))
    def meta(self,port=18080,domain='old.trycloudflare.com'):
        return agent.Metadata(domain=domain,uuid=UUID,path='/ws',local_port=port)
    def report(self,revision,ok,port,domain='old.trycloudflare.com'):
        return agent.accept_report(agent.row(self.id),agent.Report(state='online' if ok else 'error',metadata=self.meta(port,domain),process_alive=True,local_ws=True,public_ws=True,job_revision=revision,job_ok=ok))
    def test_local_failed_change_restores_and_erases_private_checkpoint(self):
        self.edit();current=db.get_node(self.id)['share_link'];r=changes.row(self.id)
        self.assertNotIn(UUID,r['checkpoint']);self.assertNotIn('checkpoint',changes.public(self.id))
        with self.assertRaises(HTTPException):self.edit(address='other.example.com')
        self.assertFalse(changes.probe_result(self.id,self.link,False)) # stale result
        self.assertTrue(changes.probe_result(self.id,current,False))
        self.assertEqual(db.get_node(self.id)['share_link'],self.link)
        self.assertEqual(changes.row(self.id)['state'],'verifying_rollback')
        changes.probe_result(self.id,self.link,True)
        self.assertEqual(changes.row(self.id)['state'],'rolled_back')
        self.assertEqual(changes.row(self.id)['checkpoint'],'')
        self.assertEqual(db.get_node(self.id)['uuid'],UUID)
    def test_local_parameters_remain_in_desired_config_for_future_read_jobs(self):
        self.edit(address='new.example.com',client_port=8443)
        desired=json.loads(agent.row(self.id)['desired_json'])
        self.assertEqual(desired['client_address'],'new.example.com');self.assertEqual(desired['client_port'],8443)
        changes.probe_result(self.id,db.get_node(self.id)['share_link'],True)
        agent.enqueue(self.id,'read',desired)
        self.report(2,True,18080)
        self.assertEqual(db.get_node(self.id)['client_address'],'new.example.com')
    def test_equal_effective_link_still_schedules_verification(self):
        body=agent.AgentEditBody(remark='Reliability QA',node_port=18080,client_address='old.trycloudflare.com',client_port=443)
        link=deploy.make_link(dict(domain='old.trycloudflare.com',uuid=UUID,path='/ws'),body)
        db.update_node(self.id,client_address='old.trycloudflare.com',share_link=link)
        agent.edit(self.id,agent.AgentEditBody(remark='Reliability QA',node_port=18080,client_address='',client_port=443))
        self.assertEqual(db.get_node(self.id)['share_link'],link)
        agent.schedule_probe.assert_called_with(self.id)
    def test_temporary_domain_rotation_retargets_inflight_change(self):
        self.edit();old=db.get_node(self.id)['share_link']
        agent.accept_report(agent.row(self.id),agent.Report(state='online',metadata=self.meta(domain='rotated.trycloudflare.com'),process_alive=True,local_ws=True,public_ws=True))
        link=db.get_node(self.id)['share_link']
        self.assertEqual(changes.row(self.id)['target_link'],link)
        self.assertFalse(changes.probe_result(self.id,old,False))
        self.assertTrue(changes.probe_result(self.id,link,False))
        node=db.get_node(self.id)
        self.assertEqual(node['client_address'],'old.example.com')
        self.assertIn('sni=rotated.trycloudflare.com',node['share_link'])
    def test_success_and_unverified_baseline_do_not_rollback(self):
        self.edit();link=db.get_node(self.id)['share_link'];changes.probe_result(self.id,link,True)
        self.assertEqual(changes.row(self.id)['state'],'succeeded')
        c=db.get_conn();c.execute("UPDATE node_agents SET probe_status='failed' WHERE node_id=?",(self.id,));c.commit()
        self.edit(address='unknown.example.com');link=db.get_node(self.id)['share_link']
        self.assertFalse(changes.probe_result(self.id,link,False))
        self.assertEqual(db.get_node(self.id)['client_address'],'unknown.example.com')
        self.assertEqual(changes.row(self.id)['state'],'unverified')
    def test_actual_probe_worker_requires_two_failures_then_verifies_restoration(self):
        self.edit()
        with patch.object(node_probe,'probe_link',side_effect=[ValueError('fixture failure'),ValueError('fixture failure'),{'latency_ms':12}]) as probe:
            self.assertTrue(self.real_schedule(self.id))
            for _ in range(120):
                if self.id not in agent.PROBING:break
                time.sleep(.05)
            self.assertEqual(probe.call_count,2)
            self.assertEqual(changes.row(self.id)['state'],'verifying_rollback')
            self.assertEqual(db.get_node(self.id)['share_link'],self.link)
            self.real_schedule(self.id)
            for _ in range(100):
                if self.id not in agent.PROBING:break
                time.sleep(.02)
            self.assertEqual(changes.row(self.id)['state'],'rolled_back')
            self.assertEqual(probe.call_count,3)
    def test_remote_success_then_failure_queues_new_revision_and_restores_with_fresh_domain(self):
        self.edit(port=18081,address='new.example.com',client_port=8443)
        self.assertEqual(changes.row(self.id)['state'],'awaiting_node')
        self.report(2,True,18081,'new.trycloudflare.com')
        link=db.get_node(self.id)['share_link'];self.assertEqual(changes.row(self.id)['state'],'verifying')
        changes.probe_result(self.id,link,False)
        self.assertEqual(changes.row(self.id)['state'],'rolling_back')
        job=db.get_conn().execute('SELECT * FROM agent_jobs WHERE node_id=? AND revision=3',(self.id,)).fetchone()
        self.assertEqual(json.loads(job['config_json'])['node_port'],18080)
        self.report(3,True,18080,'restored.trycloudflare.com')
        node=db.get_node(self.id)
        self.assertEqual(node['client_address'],'old.example.com');self.assertEqual(node['node_port'],18080)
        self.assertIn('sni=restored.trycloudflare.com',node['share_link'])
        changes.probe_result(self.id,node['share_link'],True)
        self.assertEqual(changes.row(self.id)['state'],'rolled_back')
        self.report(2,True,18081,'new.trycloudflare.com')
        self.assertEqual(db.get_node(self.id)['node_port'],18080)
    def test_failed_remote_job_restores_desired_config_and_can_explicitly_retry(self):
        self.edit(port=18081);self.report(2,False,18080)
        self.assertEqual(json.loads(agent.row(self.id)['desired_json'])['node_port'],18080)
        self.assertEqual(db.get_node(self.id)['share_link'],self.link)
        self.assertTrue(changes.prepare_retry(self.id))
        self.assertEqual(changes.retry_config(self.id)['node_port'],18081)
    def test_failed_rollback_stops_without_loop(self):
        self.edit();changes.probe_result(self.id,db.get_node(self.id)['share_link'],False)
        self.assertFalse(changes.probe_result(self.id,self.link,False))
        self.assertEqual(changes.row(self.id)['state'],'rollback_failed')
        self.assertEqual(changes.row(self.id)['checkpoint'],'')
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM agent_jobs').fetchone()[0],1)
    def test_events_deduplicate_offline_recovery_and_survive_notifications_disabled(self):
        events.health(self.id,'online',True,True,True);events.health(self.id,'online',True,True,True);db.get_conn().commit()
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM node_events').fetchone()[0],1)
        c=db.get_conn();c.execute("UPDATE node_agents SET last_report_at='2020-01-01T00:00:00Z'");c.commit()
        events.tick();events.tick();events.health(self.id,'online',True,True,True);c.commit()
        self.assertEqual(c.execute('SELECT count(*) FROM node_events').fetchone()[0],3)
        self.assertFalse(telemetry.config().get('enabled'))
        db.delete_node(self.id)
        self.assertEqual(c.execute('SELECT count(*) FROM node_events').fetchone()[0],0)
    def test_cycle_month_end_leap_year_and_year_boundary(self):
        self.assertEqual(quotas.cycle(31,date(2024,2,29)),(date(2024,2,29),date(2024,3,31)))
        self.assertEqual(quotas.cycle(31,date(2026,3,1)),(date(2026,2,28),date(2026,3,31)))
        self.assertEqual(quotas.cycle(15,date(2026,1,1)),(date(2025,12,15),date(2026,1,15)))
        for opts in [{'reset_day':0},{'reset_day':32},{'limit_bytes':-1},{'warning_percent':101},{'count_mode':'host'}]:
            with self.assertRaises(ValidationError):quotas.Rule(**opts)
    def test_quota_cycle_usage_directions_and_disable_preserve_counters(self):
        c=db.get_conn()
        for day,rx,tx in [('2026-02-27',1000,2000),('2026-02-28',10,20),('2026-03-01',30,40),('2026-03-31',500,600)]:
            c.execute('INSERT INTO traffic_daily VALUES (?,?,?,?)',(self.id,day,rx,tx))
        c.commit();quotas.configure(self.id,quotas.Rule(limit_bytes=200,reset_day=31))
        q=quotas.usage(self.id,date(2026,3,1));self.assertEqual(q['used_bytes'],100);self.assertEqual(q['remaining_bytes'],100)
        quotas.configure(self.id,quotas.Rule(limit_bytes=200,reset_day=31,count_mode='tx'))
        self.assertEqual(quotas.usage(self.id,date(2026,3,1))['used_bytes'],60)
        quotas.configure(self.id,quotas.Rule())
        self.assertEqual(c.execute('SELECT count(*) FROM traffic_daily').fetchone()[0],4)
    def test_quota_alerts_once_per_threshold_and_cycle_even_when_tg_enabled_later(self):
        quotas.configure(self.id,quotas.Rule(limit_bytes=100,warning_percent=80))
        start,end=quotas.cycle(1)
        c=db.get_conn();c.execute('INSERT INTO traffic_daily VALUES (?,?,90,0)',(self.id,start.isoformat()));c.commit()
        quotas.tick();quotas.tick()
        self.assertEqual(c.execute('SELECT count(*) FROM traffic_quota_alerts').fetchone()[0],1)
        self.assertEqual(c.execute('SELECT count(*) FROM notification_queue').fetchone()[0],0)
        db.set_setting('telegram_config',json.dumps({'enabled':True,'chat_id':'123'}))
        quotas.tick();quotas.tick()
        self.assertEqual(c.execute('SELECT count(*) FROM notification_queue').fetchone()[0],1)
        c.execute('UPDATE traffic_daily SET rx=110');c.commit();quotas.tick();quotas.tick()
        self.assertEqual(c.execute('SELECT count(*) FROM notification_queue').fetchone()[0],2)
        quotas.configure(self.id,quotas.Rule(limit_bytes=100,warning_percent=80));quotas.tick()
        self.assertEqual(c.execute('SELECT count(*) FROM notification_queue').fetchone()[0],2)
        with patch.object(quotas,'cycle',return_value=(date(2030,1,1),date(2030,2,1))):
            self.assertEqual(quotas.usage(self.id)['used_bytes'],0)
        db.delete_node(self.id)
        self.assertEqual(c.execute('SELECT count(*) FROM traffic_quotas').fetchone()[0],0)

class ReliabilityHttpTests(unittest.TestCase):
    setUpClass=classmethod(st.SubscriptionTests.setUpClass.__func__)
    tearDownClass=classmethod(st.SubscriptionTests.tearDownClass.__func__)
    call=st.SubscriptionTests.call
    create_agent=st.SubscriptionTests.create_agent
    enroll_agent=st.SubscriptionTests.enroll_agent
    agent_call=st.SubscriptionTests.agent_call
    initial_success=st.SubscriptionTests.initial_success
    def test_owner_permissions_validation_and_no_private_checkpoint_exposure(self):
        node_id,_,_=self.initial_success()
        for path in [f'/api/nodes/{node_id}/events',f'/api/traffic/nodes/{node_id}/quota']:
            self.assertEqual(self.call(path,authed=False)[0],401)
        path=f'/api/traffic/nodes/{node_id}/quota'
        self.assertEqual(self.call(path,'PUT',{'limit_bytes':100},origin='https://wrong.example')[0],403)
        self.assertEqual(self.call(path,'PUT',{'reset_day':32})[0],422)
        code,q=self.call(path,'PUT',{'limit_bytes':1024,'reset_day':31});self.assertEqual(code,200,q);self.assertEqual(q['limit_bytes'],1024)
        code,e=self.call(f'/api/nodes/{node_id}/events');self.assertEqual(code,200,e);self.assertTrue(e['events'])
        self.assertNotIn('checkpoint',json.dumps(e));self.assertNotIn('token',json.dumps(e))
        self.assertEqual(self.call('/api/nodes/999999/events')[0],404)
