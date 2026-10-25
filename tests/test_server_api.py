import unittest
import test_api


class ServerApiTests(unittest.TestCase):
    setUpClass=classmethod(test_api.ApiTests.setUpClass.__func__)
    tearDownClass=classmethod(test_api.ApiTests.tearDownClass.__func__)
    call=test_api.ApiTests.call

    def test_owner_only_inventory_tasks_recovery_and_credentials_do_not_leak(self):
        for path in ('/api/servers','/api/tasks','/api/recoveries'):
            code,_=self.call(path,authed=False);self.assertEqual(code,401)
        code,_=self.call('/api/recoveries/missing/connection','PATCH',{'client_port':33003},authed=False);self.assertEqual(code,401)
        code,_=self.call('/api/recoveries/missing/connection','PATCH',{'client_port':33003},origin='https://wrong.example');self.assertEqual(code,403)
        code,s=self.call('/api/ssh/servers','POST',{'name':'Canonical API host'});self.assertEqual(code,200)
        sid=s['id']
        code,data=self.call('/api/servers/'+sid);self.assertEqual(code,200,data);self.assertEqual(data['name'],'Canonical API host')
        self.assertNotIn('credentials',data);self.assertNotIn('token_secret',str(data))
        code,_=self.call('/api/servers/'+sid,'PUT',{'name':'Invalid origin'},origin='https://wrong.example');self.assertEqual(code,403)
        code,_=self.call('/api/servers/'+sid,'PUT',{'name':'Bad URL','control_url':'https://user:secret@example.com'});self.assertEqual(code,422)
        code,data=self.call('/api/servers/'+sid,'PUT',{'name':'Canonical API host','control_url':'https://control.example.com','fallback_urls':['https://backup.example.com']});self.assertEqual(code,200,data)
        self.assertEqual(data['fallback_urls'],['https://backup.example.com'])
        code,node=self.call('/api/servers/'+sid+'/nodes','POST',{'name':'Manual child','node_port':18081,'tunnel_mode':'temp','auto_install':False});self.assertEqual(code,200,node)
        self.assertIn('control.example.com',node['command'])
        code,data=self.call('/api/servers/'+sid);self.assertEqual(code,200);self.assertEqual([n['id'] for n in data['nodes']],[node['id']])
        code,_=self.call('/api/servers/'+sid+'/recoveries','POST',{'node_ids':[node['id']],'confirmed':False});self.assertEqual(code,400)
        code,_=self.call('/api/servers/'+sid+'/recoveries','POST',{'node_ids':[node['id']],'confirmed':True});self.assertEqual(code,409,'SSH must be configured before restoration')
        code,data=self.call('/api/tasks?kind=node&limit=1');self.assertEqual(code,200);self.assertEqual(len(data['items']),1)
        code,_=self.call('/api/tasks?limit=1000');self.assertEqual(code,422)

    def test_monitor_is_removed_and_old_entry_redirects_to_traffic(self):
        import urllib.request
        request=urllib.request.Request(self.base+'/monitor',headers={'Cookie':'sbmgmt='+self.cookie})
        with urllib.request.urlopen(request,timeout=10) as r:
            self.assertIn('/static/index.html#traffic-module',r.url)
        for path in ('/api/monitor/themes','/api/monitor/latency','/api/monitor/hosts','/agent/assets/monitor-publish.sh'):
            code,_=self.call(path);self.assertEqual(code,404)

    def test_subscription_policy_is_explicit_and_origin_checked(self):
        code,sub=self.call('/api/subscriptions','POST',{'name':'Health API','node_ids':[]});self.assertEqual(code,200,sub)
        path='/api/subscriptions/'+str(sub['id'])+'/health'
        code,data=self.call(path);self.assertEqual(code,200);self.assertFalse(data['policy']['enabled'])
        code,_=self.call(path,'PUT',{'enabled':True},authed=False);self.assertEqual(code,401)
        code,_=self.call(path,'PUT',{'enabled':True},origin='https://wrong.example');self.assertEqual(code,403)
        code,_=self.call(path,'PUT',{'enabled':True,'fail_count':1});self.assertEqual(code,422)
        code,data=self.call(path,'PUT',{'enabled':True,'fail_count':3,'fail_minutes':5,'recover_count':2,'recover_minutes':5});self.assertEqual(code,200,data)
        self.assertTrue(data['policy']['enabled']);self.assertEqual(data['nodes'],[])
