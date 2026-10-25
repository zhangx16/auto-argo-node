import json,time,unittest
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from app import db,telemetry,notify_events,events,sources,subscriptions
import test_telemetry

class NotificationTests(unittest.TestCase):
    setUp=test_telemetry.TelemetryTests.setUp
    tearDown=test_telemetry.TelemetryTests.tearDown

    def rows(self):return db.get_conn().execute('SELECT * FROM notification_queue ORDER BY id').fetchall()

    def test_html_escape_safe_link_and_message_length(self):
        db.set_setting('panel_url','https://panel.example.com')
        text=telemetry.message_text('⚠️ 测试',[('节点','<a href="evil">&测试</a>')])
        p=telemetry.payload(text,{'chat_id':'123'},'task-module')
        self.assertEqual(p['parse_mode'],'HTML');self.assertIn('&lt;a',p['text']);self.assertNotIn('<a href',p['text'])
        self.assertEqual(p['reply_markup']['inline_keyboard'][0][0]['url'],'https://panel.example.com/#task-module')
        db.set_setting('panel_url','https://panel.example.com/?secret=value')
        self.assertNotIn('reply_markup',telemetry.payload(text,{'chat_id':'123'}))
        telemetry.enqueue('long','😀'*5000)
        self.assertLessEqual(len(self.rows()[0]['text'].encode('utf-16-le'))//2,3400)

    def test_node_notification_opens_server_workspace_and_old_route_redirects(self):
        db.set_setting('panel_url','https://panel.example.com')
        direct=telemetry.payload('test',{'chat_id':'123'},'node:15')['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(direct['url'],'https://panel.example.com/static/index.html?node=15#server-module')
        old=telemetry.payload('test',{'chat_id':'123'},'node-module')['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(old['url'],'https://panel.example.com/#server-module')
        self.assertIn('服务器',old['text'])

    def test_monitor_notification_routes_use_standalone_page(self):
        db.set_setting('panel_url','https://panel.example.com')
        for route,path in [('traffic-module','/#traffic-module'),('resources-module','/#server-module')]:
            result=telemetry.payload('test',{'chat_id':'123'},route)
            self.assertEqual(result['reply_markup']['inline_keyboard'][0][0]['url'],'https://panel.example.com'+path)

    def test_event_baseline_category_and_repair_cooldown(self):
        cfg={'enabled':True}
        events.add(self.id,'job','配置版本 1 执行失败，保留原监听配置','warning');notify_events.tick(cfg)
        self.assertEqual(len(self.rows()),0) # rollout never replays old events
        events.add(self.id,'job','配置版本 2 执行失败，保留原监听配置','warning')
        events.add(self.id,'repair','自动修复次数增至 1，当前上报状态 repairing','warning')
        events.add(self.id,'repair','自动修复次数增至 2，当前上报状态 repairing','warning')
        notify_events.tick(cfg);notify_events.tick(cfg)
        self.assertEqual(len(self.rows()),2)
        self.assertEqual(self.rows()[0]['category'],'tasks')
        events.add(self.id,'job','配置版本 3 执行成功，等待转发验证')
        notify_events.tick(cfg|{'alerts_tasks':False});self.assertEqual(len(self.rows()),2)

    def test_delivery_priority_dedup_and_disabled_category(self):
        cfg={'enabled':True,'chat_id':'123','summary_enabled':False,'alerts_resources':False}
        db.set_setting('telegram_config',json.dumps(cfg))
        telemetry.enqueue('summary','summary',priority=0)
        telemetry.enqueue('urgent','urgent',priority=2)
        telemetry.enqueue('resource','resource',category='resources',priority=2)
        with patch.object(telemetry,'telegram',return_value={} ) as send:
            telemetry.tick();self.assertIn('urgent',send.call_args.args[1]['text'])
            telemetry.enqueue('urgent','duplicate')
            self.assertEqual([r['event_key'] for r in self.rows()],['summary'])

    def test_source_two_completed_failures_and_recovery(self):
        subscriptions.init();sources.init();c=db.get_conn()
        c.execute("INSERT INTO external_sources(id,name,url_cipher,interval_minutes,last_checked,last_error) VALUES(1,'来源测试','secret-url-not-to-send',60,'2026-10-23T00:00:00Z','secret-error-not-to-send')")
        cfg={'enabled':True};notify_events.subscription_notifications(cfg);self.assertFalse(self.rows())
        c.execute("UPDATE external_sources SET last_checked='2026-10-23T01:00:00Z'")
        with patch.object(sources,'RUNNING',1):notify_events.subscription_notifications(cfg)
        self.assertFalse(self.rows())
        notify_events.subscription_notifications(cfg);self.assertEqual(len(self.rows()),1)
        self.assertNotIn('secret',self.rows()[0]['text'])
        notify_events.subscription_notifications(cfg);self.assertEqual(len(self.rows()),1)
        c.execute("UPDATE external_sources SET last_error='',last_success='2026-10-23T01:00:01Z'")
        notify_events.subscription_notifications(cfg);self.assertEqual(len(self.rows()),2);self.assertIn('恢复',self.rows()[1]['text'])

    def test_expiry_distinct_stages_no_subscription_token(self):
        subscriptions.init();c=db.get_conn();now=datetime.now(timezone.utc)
        expiry=(now+timedelta(hours=24)).isoformat()
        c.execute('INSERT INTO subscriptions(name,expires_at,token_version,created_at,updated_at) VALUES(?,?,?,?,?)',('测试订阅',expiry,'secret-sub-token',db.utcnow(),db.utcnow()))
        notify_events.subscription_notifications({'enabled':True});notify_events.subscription_notifications({'enabled':True})
        self.assertEqual(len(self.rows()),1);self.assertIn('即将到期',self.rows()[0]['text']);self.assertNotIn('secret',self.rows()[0]['text'])
        with patch.object(notify_events.time,'time',return_value=(now+timedelta(hours=25)).timestamp()):notify_events.subscription_notifications({'enabled':True})
        self.assertEqual(len(self.rows()),2);self.assertIn('已到期',self.rows()[1]['text'])

    def test_pressure_sustained_gap_and_hysteresis(self):
        c=db.get_conn()
        c.execute("INSERT INTO ssh_servers(id,name,created_at) VALUES ('fixture','Host',?)",(db.utcnow(),))
        c.execute("INSERT INTO ssh_nodes VALUES (?,'fixture')",(self.id,))
        remote=[{'id':1,'online':True,'metrics':{'cpu':95}}]
        def sample(t):
            c.execute('INSERT OR REPLACE INTO node_resource_latest VALUES (?,?,?)',(self.id,t,json.dumps({'cpu_percent':remote[0]['metrics']['cpu']})))
            with patch.object(notify_events.time,'time',return_value=t):notify_events.resource_notifications({'enabled':True})
        for t in range(1000,1300,60):sample(t)
        self.assertFalse(self.rows());sample(1300);self.assertEqual(len(self.rows()),1)
        sample(1360);self.assertEqual(len(self.rows()),1)
        remote[0]['metrics']['cpu']=85;sample(1420);self.assertEqual(len(self.rows()),1)
        remote[0]['metrics']['cpu']=79;sample(1480);self.assertEqual(len(self.rows()),2)
        remote[0]['metrics']['cpu']=99;sample(1540);sample(1900)
        self.assertEqual(len(self.rows()),2) # missing samples reset the 5-minute window

    def test_old_clients_preserve_category_preferences(self):
        db.set_setting('telegram_config',json.dumps({'alerts_resources':False,'token':'encrypted-fixture'}))
        with patch.object(telemetry,'telegram',return_value={'username':'fixture'}):
            result=telemetry.configure(telemetry.Config(chat_id='123'))
        self.assertFalse(result['alerts_resources']);self.assertTrue(result['alerts_health'])

    def test_collector_outage_does_not_suppress_forwarding_failure(self):
        c=db.get_conn();c.execute("UPDATE node_agents SET health_mode='monitor',probe_status='failed'")
        db.set_setting('telegram_config',json.dumps({'enabled':True,'chat_id':'123','summary_enabled':False}))
        with patch('app.monitor.collector_available',return_value=False),patch.object(telemetry,'telegram',return_value={}) as send:
            telemetry.tick()
            c.execute('UPDATE notification_states SET pending_since=?',(int(time.time())-61,));c.commit()
            telemetry.tick()
        self.assertTrue(send.called);self.assertIn('实际协议转发检查失败',send.call_args.args[1]['text'])

    def test_recovery_has_elapsed_time_and_supersedes_queued_health(self):
        c=db.get_conn();now=int(time.time())
        c.execute("INSERT INTO notification_states VALUES (?,'offline','online',?)",(self.id,now-61))
        notify_events.put(f'outage:{self.id}',now-360)
        telemetry.enqueue(f'node:{self.id}:offline:old','old outage',category='health',priority=2)
        db.set_setting('telegram_config',json.dumps({'enabled':True,'chat_id':'123','summary_enabled':False}))
        with patch.object(telemetry,'telegram',return_value={}) as send:telemetry.tick()
        self.assertEqual(send.call_count,1);self.assertIn('已恢复',send.call_args.args[1]['text']);self.assertIn('6 分钟',send.call_args.args[1]['text'])
        self.assertFalse(self.rows())

    def test_beijing_time_rollover_and_queued_legacy_messages(self):
        self.assertEqual(telemetry.china_time('2026-10-23T20:30:00Z'),'2026-10-24 04:30:00（北京时间 UTC+8）')
        text=telemetry.message_text('测试',[('最后心跳','2026-10-23T20:30:00Z')])
        self.assertIn('2026-10-24 04:30:00',text)
        self.assertNotIn('T20:30:00Z',text)
        p=telemetry.payload('测试\n最后心跳：2026-10-23T20:30:00Z\n🕒 2026-10-23T20:30:00Z · UTC',{'chat_id':'123'})
        self.assertEqual(p['text'].count('2026-10-24 04:30:00'),2)
        self.assertNotIn(' · UTC',p['text'])
        self.assertIn('今日用量（UTC）',telemetry.summary_text())
