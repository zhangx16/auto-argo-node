import json,sqlite3,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from app import db,telemetry,agent,deploy,cloudflare,fixed_jobs

class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=getattr(db._local,'conn',None)
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');db._local.conn=c
        self.patch=patch.object(db,'DATA_DIR',Path(self.tmp.name));self.patch.start();Path(self.tmp.name,'secret.key').write_text('fixture-not-a-real-key')
        db.init_db();deploy.init();agent.init();self.id=db.create_node('Telemetry QA',18080,'www.amd.com','','fixture-token')['id']
        c.execute("UPDATE nodes SET status='online'");c.execute("INSERT INTO node_agents(node_id,desired_json,actual_port,bound,last_report_at,health_status) VALUES (?,?,?,1,?,'online')",(self.id,'{}',18080,db.utcnow()));c.commit()
    def tearDown(self):
        db._local.conn.close();db._local.conn=self.old;self.patch.stop();self.tmp.cleanup()
    def record(self,epoch,rx,tx):
        telemetry.record(self.id,telemetry.Traffic(epoch=epoch,rx=rx,tx=tx));db.get_conn().commit()
    def test_deltas_duplicates_counter_restart_and_out_of_order(self):
        self.record('a',100,200);self.record('a',150,300);self.record('a',150,300);self.record('a',120,250)
        self.assertEqual(telemetry.summary()['totals']['today_rx'],50)
        self.record('b',10,20);self.record('b',15,25)
        t=telemetry.summary()['totals'];self.assertEqual((t['rx'],t['tx']),(65,125))
        db.delete_node(self.id);self.assertEqual(telemetry.summary()['totals']['rx'],0)
    def test_notification_transition_dedup_recovery_and_retry(self):
        db.set_setting('telegram_config',json.dumps({'enabled':True,'chat_id':'123','summary_enabled':False}))
        c=db.get_conn();c.execute("UPDATE node_agents SET last_report_at='2020-01-01T00:00:00Z'");c.commit()
        with patch.object(telemetry,'telegram',return_value={'message_id':1}) as send:
            telemetry.tick();telemetry.tick();telemetry.tick();self.assertEqual(send.call_count,1);self.assertIn('心跳停止',send.call_args.args[1]['text'])
            c.execute('UPDATE node_agents SET last_report_at=?',(db.utcnow(),));c.commit();telemetry.tick()
            c.execute('UPDATE notification_states SET pending_since=?',(int(time.time())-61,));c.commit();telemetry.tick();self.assertEqual(send.call_count,2);self.assertIn('已恢复',send.call_args.args[1]['text'])
        telemetry.enqueue('fixture','test')
        with patch.object(telemetry,'telegram',side_effect=HTTPException(502,'redacted')) as send:
            telemetry.tick();telemetry.tick();self.assertEqual(send.call_count,1)
        q=c.execute('SELECT * FROM notification_queue').fetchone();self.assertEqual(q['attempts'],1);self.assertGreater(q['due'],time.time());self.assertIn('重试',telemetry.status()['last_error'])
    def test_config_encrypted_no_secret_returned_preserve_and_chat_change_queue_clear(self):
        with patch.object(telemetry,'telegram',return_value={'username':'fixture_bot'}):
            s=telemetry.configure(telemetry.Config(token='123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_fixture',chat_id='123',enabled=True))
            self.assertNotIn('token',s);self.assertNotIn('ABCDEFGHIJKLMNOPQRSTUVWXYZ',db.get_setting('telegram_config'))
            encrypted=telemetry.config()['token'];self.assertEqual(cloudflare.decrypt(encrypted),'123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_fixture')
            telemetry.enqueue('old','pending')
            telemetry.configure(telemetry.Config(chat_id='456',enabled=True));self.assertEqual(telemetry.config()['token'],encrypted)
            self.assertEqual(telemetry.status()['queued'],0)
    def test_queue_bound_summary_and_shell_scripts(self):
        for i in range(210):telemetry.enqueue(str(i),'fixture')
        self.assertEqual(telemetry.status()['queued'],200);self.assertIn('非厂商账单',telemetry.summary_text())
        import subprocess
        for p in ['node-traffic.sh','node-agent.sh']:
            subprocess.run(['sh','-n',str(Path(telemetry.__file__).parent/p)],check=True)
        for kind in ['reality','hysteria2','vless-ws-argo']:
            script=fixed_jobs.upgrade(7,18080,kind);subprocess.run(['sh','-n'],input=script.encode(),check=True)
            if kind!='vless-ws-argo':self.assertIn('cat "$DIR/direct.json"',script)
    def test_compact_library_maintenance_entry_and_dependency_closure(self):
        text=deploy.installer_text()
        self.assertEqual(text.splitlines()[-1],'main')
        for removed in ['_add_tuic()','_add_shadowsocks_menu()','_main_menu()','_do_update_xray()']:
            self.assertNotIn(removed,text)
        self.assertIn('_add_argo_vless_ws()',text)
        self.assertIn('case "${ARGO_ACTION:-}"',text)
        import subprocess
        subprocess.run(['bash','-n'],input=text.encode(),check=True)

    def test_auth_and_sampling_validation(self):
        from pydantic import ValidationError
        for kwargs in [{'epoch':'a','rx':-1,'tx':0},{'epoch':'a','rx':0,'tx':0,'scope':'host'}]:
            with self.assertRaises(ValidationError):telemetry.Traffic(**kwargs)
        with self.assertRaises(HTTPException):telemetry.configure(telemetry.Config(enabled=True))
