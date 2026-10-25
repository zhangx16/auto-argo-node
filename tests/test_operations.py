import json
import time
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from app import agent,db,operations,upgrades,panel_health,telemetry,notify_events
import test_reliability

class OperationsTests(unittest.TestCase):
    setUp=test_reliability.ReliabilityTests.setUp
    tearDown=test_reliability.ReliabilityTests.tearDown

    def test_node_and_host_maintenance_expire_independently(self):
        c=db.get_conn()
        c.execute("INSERT INTO monitor_hosts(id,name,hub_id,hub_token,token_hash,token_secret) VALUES ('h','Host',1,'fixture','fixture','fixture')")
        c.execute('INSERT INTO monitor_nodes VALUES (?,?)',(self.id,'h'));c.commit()
        operations.set_maintenance('host','h',120,'host work')
        operations.set_maintenance('node',str(self.id),30,'node work')
        self.assertFalse(operations.permit(self.id)['allowed'])
        operations.set_maintenance('node',str(self.id),0,'')
        self.assertEqual(operations.maintenance(self.id)['scope'],'host')
        with patch('app.operations.time.time',return_value=time.time()+7300):
            operations.tick();self.assertIsNone(operations.maintenance(self.id))
        self.assertTrue(operations.permit(self.id)['allowed'])

    def test_limit_cooldown_and_latch_survive_window(self):
        now=time.time()
        for step in range(3):
            with patch('app.operations.time.time',return_value=now+step*301):
                self.assertTrue(operations.permit(self.id)['allowed'])
                self.assertFalse(operations.permit(self.id)['allowed'])
        with patch('app.operations.time.time',return_value=now+7200):
            self.assertFalse(operations.permit(self.id)['allowed'])
            self.assertTrue(operations.control(self.id)['repair']['tripped'])
        count=db.get_conn().execute("SELECT count(*) FROM node_events WHERE kind='repair_limit'").fetchone()[0]
        self.assertEqual(count,1)

    def test_pending_configuration_blocks_repair(self):
        agent.enqueue(self.id,'read',self.config)
        self.assertFalse(operations.permit(self.id)['allowed'])
        self.assertEqual(operations.policy(self.id)['attempts'],0)

    def test_diagnostics_excludes_credentials_and_stale_health(self):
        c=db.get_conn();c.execute("UPDATE node_agents SET last_report_at='2020-01-01T00:00:00Z',probe_error=?,last_job_error=? WHERE node_id=?",('secret-probe','secret-job',self.id));c.commit()
        d=operations.diagnostics(self.id);text=json.dumps(d)
        for secret in ('secret-probe','secret-job',self.config['uuid'],self.link,'fixture-token'):self.assertNotIn(secret,text)
        self.assertEqual(d['checks'][1]['state'],'unknown')

    def test_maintenance_suppresses_health_and_repair_notifications(self):
        notify_events.put('events_cursor',0)
        operations.set_maintenance('node',str(self.id),30,'test')
        from app import events
        events.add(self.id,'repair_limit','limit','warning');db.get_conn().commit()
        with patch.object(telemetry,'enqueue') as send:
            notify_events.event_notifications({'enabled':True});send.assert_not_called()

    def test_health_is_observation_only_and_stale_is_unknown(self):
        with patch.dict(panel_health.COMPONENTS,{},clear=True):
            panel_health.record('fixture',False)
            snapshot=panel_health.snapshot()
            self.assertEqual(snapshot['components']['fixture']['state'],'failed')
            with patch('app.panel_health.time.time',return_value=time.time()+200):
                self.assertEqual(panel_health.snapshot()['components']['fixture']['state'],'unknown')

    def test_batch_requires_baseline_and_rejects_duplicate(self):
        with self.assertRaises(HTTPException):upgrades.create(upgrades.BatchBody(node_ids=[self.id,self.id]))
        db.get_conn().execute("UPDATE node_agents SET probe_status='failed' WHERE node_id=?",(self.id,));db.get_conn().commit()
        with self.assertRaises(HTTPException):upgrades.create(upgrades.BatchBody(node_ids=[self.id]))

    def test_batch_cannot_advance_before_forward_verification(self):
        with patch.object(upgrades,'fingerprint',return_value='{}'):
            b=upgrades.create(upgrades.BatchBody(node_ids=[self.id]));upgrades.tick()
            item=upgrades.batches()[0]['items'][0];self.assertEqual(item['status'],'running')
            revision=item['revision'];c=db.get_conn()
            c.execute("UPDATE agent_jobs SET status='done',finished_at=? WHERE node_id=? AND revision=?",(db.utcnow(),self.id,revision))
            c.execute("UPDATE node_agents SET applied_revision=?,agent_version=8,probe_status='unknown' WHERE node_id=?",(revision,self.id));c.commit()
            upgrades.tick();self.assertEqual(upgrades.batches()[0]['items'][0]['status'],'verifying')
            upgrades.tick();self.assertEqual(upgrades.batches()[0]['status'],'running')
            c.execute("UPDATE node_agents SET probe_status='ok',probe_at=? WHERE node_id=?",('2099-01-01T00:00:00Z',self.id));c.commit()
            upgrades.tick();upgrades.tick();self.assertEqual(upgrades.batches()[0]['status'],'done')

    def test_batch_failure_or_resource_change_pauses(self):
        with patch.object(upgrades,'fingerprint',return_value='{}'):
            upgrades.create(upgrades.BatchBody(node_ids=[self.id]));upgrades.tick()
        c=db.get_conn();c.execute("UPDATE agent_jobs SET status='failed' WHERE node_id=? AND status='queued'",(self.id,));c.commit()
        upgrades.tick();self.assertEqual(upgrades.batches()[0]['status'],'paused')
        self.assertEqual(upgrades.batches()[0]['items'][0]['status'],'failed')

    def test_batch_resource_fingerprint_change_does_not_dispatch(self):
        with patch.object(upgrades,'fingerprint',return_value='{}'):upgrades.create(upgrades.BatchBody(node_ids=[self.id]))
        with patch.object(upgrades,'fingerprint',return_value='changed'):upgrades.tick()
        self.assertEqual(upgrades.batches()[0]['status'],'paused')
        self.assertFalse(db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE status='queued'").fetchone())

    def test_release_pins_do_not_contaminate_future_configuration(self):
        cfg=self.config|{'_release_sha256':'frozen','_upgrade_hashes':{'agent.sh':'fixture'}}
        rev=agent.enqueue(self.id,'upgrade',cfg)
        self.assertNotIn('_release_sha256',json.loads(agent.row(self.id)['desired_json']))
        job=json.loads(db.get_conn().execute('SELECT config_json FROM agent_jobs WHERE node_id=? AND revision=?',(self.id,rev)).fetchone()[0])
        self.assertEqual(job['_release_sha256'],'frozen')

    def test_policy_and_batch_survive_connection_restart(self):
        now=time.time()
        for offset in (0,301,602):
            with patch('app.operations.time.time',return_value=now+offset):operations.permit(self.id)
        with patch.object(upgrades,'fingerprint',return_value='{}'):upgrades.create(upgrades.BatchBody(node_ids=[self.id]))
        db.get_conn().close();db._local.conn=None;agent.init()
        self.assertTrue(operations.control(self.id)['repair']['tripped'])
        self.assertEqual(upgrades.batches()[0]['status'],'running')

    def test_batch_waits_for_inflight_baseline_instead_of_pausing(self):
        with patch.object(upgrades,'fingerprint',return_value='{}'):
            upgrades.create(upgrades.BatchBody(node_ids=[self.id]))
            db.get_conn().execute("UPDATE node_agents SET probe_status='pending' WHERE node_id=?",(self.id,));db.get_conn().commit()
            upgrades.tick()
            self.assertEqual(upgrades.batches()[0]['status'],'running')
            self.assertEqual(upgrades.batches()[0]['items'][0]['status'],'pending')
            db.get_conn().execute("UPDATE node_agents SET probe_status='ok' WHERE node_id=?",(self.id,));db.get_conn().commit()
            upgrades.tick();self.assertEqual(upgrades.batches()[0]['items'][0]['status'],'running')
