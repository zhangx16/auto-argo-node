import json,re,subprocess,unittest
import test_agent as agent_tests
from app import direct_nodes

class DirectApiTests(unittest.TestCase):
    setUpClass=classmethod(agent_tests.AgentApiTests.setUpClass.__func__)
    tearDownClass=classmethod(agent_tests.AgentApiTests.tearDownClass.__func__)
    call=agent_tests.AgentApiTests.call
    agent_call=agent_tests.AgentApiTests.agent_call
    enroll_agent=agent_tests.AgentApiTests.enroll_agent

    def installed(self,kind):
        code,created=self.call('/api/agent-nodes','POST',{'remark':'Direct '+kind,'protocol':kind,'node_port':32660,'client_port':32660,'client_address':'example.com','server_name':'www.amd.com'})
        self.assertEqual(code,200,created)
        node_id=created['id'];enroll=re.search(r'/agent/bootstrap/([A-Za-z0-9_-]+)\.sh',created['command']).group(1)
        token=self.enroll_agent(enroll)
        _,work=self.agent_call('/agent/work',token);config=json.loads(work['job']['config_json'])
        code,script=self.agent_call('/agent/job/1.sh',token);self.assertEqual(code,200)
        subprocess.run(['sh','-n'],input=script.encode(),check=True)
        meta={'protocol':kind,'type':'direct','domain':'www.amd.com','uuid':config['uuid'],'path':'/','local_port':32660}
        if kind=='reality':meta.update(public_key='A'*43,short_id='0123456789abcdef')
        body={'state':'installing','agent_version':3,'metadata':meta,'local_ws':True,'process_alive':True,'job_revision':1,'job_ok':True}
        code,ack=self.agent_call('/agent/report',token,body);self.assertEqual(code,200,ack)
        return node_id,token,meta

    def test_direct_lifecycle_and_failed_update_keeps_link(self):
        for kind in ['reality','hysteria2']:
            node_id,token,meta=self.installed(kind)
            before=next(n for n in self.call('/api/nodes')[1] if n['id']==node_id)
            self.assertEqual(before['protocol'],kind);self.assertEqual(before['status'],'online')
            self.assertTrue(before['share_link'].startswith('vless://' if kind=='reality' else 'hysteria2://'))
            bad=dict(meta,protocol='hysteria2' if kind=='reality' else 'reality')
            self.assertEqual(self.agent_call('/agent/report',token,{'state':'online','metadata':bad})[0],409)
            self.assertEqual(self.call(f'/api/agent-nodes/{node_id}/fixed','POST',{'hostname':'test.example.com'})[0],400)
            code,draft=self.call(f'/api/agent-nodes/{node_id}','PATCH',{'remark':'Updated direct','node_port':32661,'client_address':'new.example.com','client_port':32662})
            self.assertEqual(code,200,draft);self.assertEqual(draft['share_link'],before['share_link'])
            self.agent_call('/agent/job/2.sh',token)
            code,_=self.agent_call('/agent/report',token,{'state':'error','metadata':meta,'job_revision':2,'job_ok':False,'error':'port conflict'})
            self.assertEqual(code,200)
            failed=next(n for n in self.call('/api/nodes')[1] if n['id']==node_id)
            self.assertEqual(failed['share_link'],before['share_link'])
            self.assertEqual(self.call(f'/api/agent-nodes/{node_id}/retry','POST')[0],200)
            self.agent_call('/agent/job/3.sh',token);meta['local_port']=32661
            code,ack=self.agent_call('/agent/report',token,{'state':'installing','metadata':meta,'local_ws':True,'process_alive':True,'job_revision':3,'job_ok':True})
            self.assertEqual(code,200,ack)
            current=next(n for n in self.call('/api/nodes')[1] if n['id']==node_id)
            self.assertIn('@new.example.com:32662?',current['share_link']);self.assertEqual(current['uuid'],before['uuid'])
            self.assertEqual(current['node_port'],32661)
            self.assertEqual(self.call(f'/api/nodes/{node_id}?mode=force','DELETE')[0],200)
            self.assertEqual(self.agent_call('/agent/work',token)[0],401)

    def test_validation_and_subscription_formats(self):
        self.assertEqual(self.call('/api/agent-nodes','POST',{'remark':'Direct','protocol':'hysteria2','node_port':33001,'client_port':33001})[0],400)
        self.assertEqual(self.call('/api/agent-nodes','POST',{'remark':'Bad SNI','protocol':'reality','node_port':33001,'client_port':33001,'client_address':'example.com','server_name':'x;id'})[0],422)
        nodes=[self.installed(kind)[0] for kind in ['reality','hysteria2']]
        code,group=self.call('/api/subscriptions','POST',{'name':'Direct formats','node_ids':nodes});self.assertEqual(code,200,group)
        for fmt in ['raw','base64','clash','singbox']:
            code,preview=self.call(f"/api/subscriptions/{group['id']}/preview?format={fmt}")
            self.assertEqual(code,200,preview);self.assertEqual(preview['node_count'],2)
        for node_id in nodes:self.call(f'/api/nodes/{node_id}?mode=force','DELETE')
        self.call(f"/api/subscriptions/{group['id']}",'DELETE')

    def test_privileged_direct_port(self):
        for kind in ['reality','hysteria2']:
            code,node=self.call('/api/agent-nodes','POST',{'remark':'Port 443','protocol':kind,'node_port':443,'client_port':443,'client_address':'example.com'})
            self.assertEqual(code,200,node)
            self.call(f"/api/nodes/{node['id']}?mode=force",'DELETE')
        self.assertEqual(self.call('/api/agent-nodes','POST',{'remark':'Argo','node_port':443})[0],400)

    def test_owned_uninstall_payload(self):
        config={'protocol':'hysteria2','node_port':33001,'uuid':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','ws_path':'/','server_name':'www.amd.com'}
        script=direct_nodes.uninstall(7,config,2)
        subprocess.run(['sh','-n'],input=script.encode(),check=True)
        self.assertIn('panel-hysteria2-7',script)
        self.assertNotIn('argo-vless-ws-${PORT}',script)
