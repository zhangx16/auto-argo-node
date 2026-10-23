import json,time,unittest
from unittest.mock import patch
from app import db,telemetry,bot_commands
import test_telemetry

class BotCommandTests(unittest.TestCase):
    setUp=test_telemetry.TelemetryTests.setUp
    tearDown=test_telemetry.TelemetryTests.tearDown
    def config(self,**extra):return {'token':'encrypted-fixture','chat_id':'123','bot_username':'fixture_bot'}|extra
    def update(self,uid,text='/start',chat=123,age=0):return {'update_id':uid,'message':{'chat':{'id':chat,'type':'private','first_name':'QA'},'from':{'id':123,'is_bot':False},'date':int(time.time())-age,'text':text}}
    def queue(self):return db.get_conn().execute('SELECT * FROM notification_queue').fetchall()
    def test_sync_replaces_webapp_for_default_and_bound_chat(self):
        with patch.object(telemetry,'telegram',return_value=True) as send:bot_commands.sync_menu(self.config())
        menu=[c.args[1] for c in send.call_args_list if c.args[0]=='setChatMenuButton']
        self.assertEqual(menu,[{'menu_button':{'type':'commands'}},{'chat_id':'123','menu_button':{'type':'commands'}}])
        configured=[c.args[1] for c in send.call_args_list if c.args[0]=='setMyCommands']
        self.assertEqual(len(configured),4)
        self.assertEqual([c['command'] for c in configured[0]['commands']],['start','status','traffic','nodes','help'])
        with patch.object(telemetry,'telegram',return_value=True) as send:bot_commands.sync_menu(self.config(chat_id='-123'))
        self.assertEqual(sum(c.args[0]=='setChatMenuButton' for c in send.call_args_list),1)
    def test_chat_scope_old_message_and_bot_mention(self):
        updates=[self.update(1,chat=999),self.update(2,age=600),self.update(3,'/status@other_bot'),self.update(4,'/status@fixture_bot')]
        with patch.object(telemetry,'telegram',return_value=updates):bot_commands.poll(self.config())
        self.assertEqual(len(self.queue()),1);self.assertIn('节点状态',self.queue()[0]['text'])
        self.assertNotIn('fixture-token',self.queue()[0]['text']);self.assertEqual(json.loads(db.get_setting('telegram_command_cursor'))['offset'],5)
        with patch.object(telemetry,'telegram',return_value=updates):bot_commands.poll(self.config())
        self.assertEqual(len(self.queue()),1)
        self.assertEqual(len(bot_commands.recent_chats()),2)
    def test_batch_keeps_latest_and_rate_limits(self):
        with patch.object(telemetry,'telegram',return_value=[self.update(1,'/start'),self.update(2,'/traffic')]):bot_commands.poll(self.config())
        self.assertEqual(len(self.queue()),1);self.assertIn('流量简报',self.queue()[0]['text'])
        with patch.object(telemetry,'telegram',return_value=[self.update(3,'/help')]):bot_commands.poll(self.config())
        self.assertEqual(len(self.queue()),1)
        self.assertEqual(json.loads(db.get_setting('telegram_command_cursor'))['offset'],4)
    def test_commands_work_with_alert_notifications_disabled(self):
        cfg=self.config(enabled=False,summary_enabled=False);db.set_setting('telegram_config',json.dumps(cfg))
        def telegram(method,payload=None,cfg=None):
            if method=='getUpdates':return [self.update(1)]
            return {'message_id':1}
        with patch.object(telemetry,'telegram',side_effect=telegram) as send:telemetry.tick()
        sent=[c for c in send.call_args_list if c.args[0]=='sendMessage']
        self.assertEqual(len(sent),1);self.assertIn('节点管理助手',sent[0].args[1]['text'])
    def test_missing_binding_does_not_poll_and_read_only_help(self):
        with patch.object(telemetry,'telegram') as send:bot_commands.poll(self.config(chat_id=''))
        send.assert_not_called()
        text,route=bot_commands.command_text('help');self.assertIn('/start',text);self.assertIn('北京时间',text)
