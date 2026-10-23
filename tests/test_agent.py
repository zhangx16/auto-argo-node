"""Agent contracts: one-time enrollment, scoped tokens, ordered configs and link sync."""
import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
import test_api as api_tests
from app.agent_scripts import bootstrap_script

class AgentApiTests(unittest.TestCase):
    setUpClass=classmethod(api_tests.ApiTests.setUpClass.__func__)
    tearDownClass=classmethod(api_tests.ApiTests.tearDownClass.__func__)
    call=api_tests.ApiTests.call
    def create_agent(self,port=18080):
        code,data=self.call('/api/agent-nodes','POST',{'remark':'Agent QA','node_port':port,'client_address':'edge.example.com','client_port':443})
        self.assertEqual(code,200,data)
        import re
        token=re.search(r'/agent/bootstrap/([A-Za-z0-9_-]+)\.sh',data['command']).group(1)
        return data['id'],token,data
    def enroll_agent(self,token):
        code,result=self.call('/agent/enroll','POST',{'enrollment_token':token,'hostname':'qa-node','os':'alpine','arch':'x86_64','ip':'192.0.2.1'},authed=False)
        self.assertEqual(code,200,result);return result['agent_token']
    def agent_call(self,path,token,body=None):
        import urllib.request,urllib.error
        request=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers={'X-Agent-Token':token,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=10) as r:
                raw=r.read().decode()
                return r.status,json.loads(raw) if r.headers['Content-Type'].startswith('application/json') else raw
        except urllib.error.HTTPError as e:return e.code,json.load(e)
    def initial_success(self):
        id,enroll,_=self.create_agent();token=self.enroll_agent(enroll)
        code,work=self.agent_call('/agent/work',token);self.assertEqual(code,200)
        config=json.loads(work['job']['config_json']);rev=work['job']['revision']
        code,script=self.agent_call(f'/agent/job/{rev}.sh',token);self.assertEqual(code,200)
        subprocess.run(['sh','-n'],input=script.encode(),check=True)
        meta={'domain':'agent-qa.trycloudflare.com','uuid':config['uuid'],'path':config['ws_path'],'local_port':config['node_port']}
        result={'state':'online','metadata':meta,'process_alive':True,'local_ws':True,'public_ws':True,'job_revision':rev,'job_ok':True}
        code,ack=self.agent_call('/agent/report',token,result);self.assertEqual(code,200,ack)
        return id,token,meta
    def test_fixed_create_requires_configured_account(self):
        before=self.call('/api/nodes')[1]
        code,data=self.call('/api/agent-nodes','POST',{'remark':'Fixed QA','node_port':18080,'tunnel_mode':'fixed','fixed_hostname':'kr.example.com'})
        self.assertEqual(code,400,data)
        self.assertEqual(len(self.call('/api/nodes')[1]),len(before))
        code,_=self.call('/api/agent-nodes','POST',{'remark':'Invalid mode','node_port':18080,'tunnel_mode':'other'})
        self.assertEqual(code,422)

    def test_one_time_enrollment_and_rebinding_revokes_old_token(self):
        id,enroll,cmd=self.create_agent();token=self.enroll_agent(enroll)
        code,_=self.call('/agent/enroll','POST',{'enrollment_token':enroll},authed=False);self.assertEqual(code,410)
        code,_=self.agent_call('/agent/work','wrong-token');self.assertEqual(code,401)
        code,command=self.call(f'/api/agent-nodes/{id}/enrollment','POST');self.assertEqual(code,200)
        import re
        new=re.search(r'/agent/bootstrap/([A-Za-z0-9_-]+)\.sh',command['command']).group(1)
        self.enroll_agent(new);code,_=self.agent_call('/agent/work',token);self.assertEqual(code,401)
    def test_heartbeat_updates_domain_and_preserves_preferred_address(self):
        id,token,meta=self.initial_success();meta['domain']='new-domain.trycloudflare.com'
        code,result=self.agent_call('/agent/report',token,{'state':'online','metadata':meta,'process_alive':True,'local_ws':True,'public_ws':True,'repair_count':1})
        self.assertEqual(code,200,result)
        _,nodes=self.call('/api/nodes');node=next(x for x in nodes if x['id']==id)
        self.assertIn('@edge.example.com:443?',node['share_link']);self.assertIn('sni=new-domain.trycloudflare.com',node['share_link']);self.assertEqual(node['repair_count'],1);self.assertEqual(node['management'],'agent')
    def test_port_edit_queues_job_without_changing_actual_port(self):
        id,token,meta=self.initial_success()
        code,node=self.call(f'/api/agent-nodes/{id}','PATCH',{'remark':'Renamed','node_port':18081,'client_address':'new.example.com','client_port':8443})
        self.assertEqual(code,200,node);self.assertEqual(node['node_port'],18080);self.assertEqual(node['desired_port'],18081);self.assertIn('@new.example.com:8443?',node['share_link'])
        _,work=self.agent_call('/agent/work',token);self.assertEqual(work['job']['operation'],'update');self.assertEqual(json.loads(work['job']['config_json'])['node_port'],18081)
        meta['local_port']=18081;meta['domain']='updated.trycloudflare.com'
        code,ack=self.agent_call('/agent/report',token,{'state':'online','metadata':meta,'process_alive':True,'local_ws':True,'public_ws':True,'job_revision':work['job']['revision'],'job_ok':True});self.assertEqual(code,200,ack)
        _,nodes=self.call('/api/nodes');self.assertEqual(next(n for n in nodes if n['id']==id)['node_port'],18081)
    def test_reject_wrong_uuid_and_port(self):
        id,token,meta=self.initial_success();bad=dict(meta,uuid='341cad6d-9bf5-4b09-9c96-a7879ebc8fe1')
        code,_=self.agent_call('/agent/report',token,{'state':'online','metadata':bad});self.assertEqual(code,409)
        bad=dict(meta,local_port=18082);code,_=self.agent_call('/agent/report',token,{'state':'online','metadata':bad});self.assertEqual(code,409)
    def test_replayed_job_does_not_restore_stale_domain(self):
        id,token,meta=self.initial_success();old=dict(meta);meta['domain']='fresh.trycloudflare.com'
        self.agent_call('/agent/report',token,{'state':'online','metadata':meta,'local_ws':True,'public_ws':True})
        self.agent_call('/agent/report',token,{'state':'online','metadata':old,'job_revision':1,'job_ok':True})
        _,nodes=self.call('/api/nodes');self.assertEqual(next(n for n in nodes if n['id']==id)['sni'],'fresh.trycloudflare.com')
    def test_can_edit_unexecuted_install_command_and_delete_pending_record(self):
        id,token,_=self.create_agent()
        code,node=self.call(f'/api/agent-nodes/{id}','PATCH',{'remark':'Before install','node_port':19000,'client_address':'','client_port':443});self.assertEqual(code,200,node);self.assertEqual(node['node_port'],19000)
        code,_=self.call(f'/api/nodes/{id}','DELETE');self.assertEqual(code,200)
        code,_=self.call('/agent/enroll','POST',{'enrollment_token':token},authed=False);self.assertEqual(code,410)
    def test_failed_config_keeps_old_link_and_requires_explicit_retry(self):
        id,token,meta=self.initial_success()
        self.call(f'/api/agent-nodes/{id}','PATCH',{'remark':'Keep old','node_port':18085,'client_address':'edge.example.com','client_port':443})
        _,work=self.agent_call('/agent/work',token);rev=work['job']['revision']
        code,_=self.agent_call('/agent/report',token,{'state':'error','metadata':meta,'job_revision':rev,'job_ok':False,'error':'forced update failure'});self.assertEqual(code,200)
        _,nodes=self.call('/api/nodes');n=next(n for n in nodes if n['id']==id)
        self.assertEqual(n['node_port'],18080);self.assertEqual(n['status'],'error');self.assertTrue(n['share_link'])
        _,work=self.agent_call('/agent/work',token);self.assertIsNone(work['job'])
        code,_=self.call(f'/api/agent-nodes/{id}/retry','POST');self.assertEqual(code,200)
        _,work=self.agent_call('/agent/work',token);self.assertEqual(work['job']['operation'],'update');self.assertEqual(json.loads(work['job']['config_json'])['node_port'],18085)
    def test_stale_heartbeat_is_offline_even_if_job_was_running(self):
        import sqlite3
        from pathlib import Path
        id,token,meta=self.initial_success()
        self.call(f'/api/agent-nodes/{id}','PATCH',{'remark':'Offline','node_port':18086,'client_address':'edge.example.com','client_port':443})
        _,work=self.agent_call('/agent/work',token);self.agent_call(f"/agent/job/{work['job']['revision']}.sh",token)
        c=sqlite3.connect(Path(self.tmp.name)/'app.db');c.execute("UPDATE node_agents SET health_mode='legacy',last_report_at='2020-01-01T00:00:00Z' WHERE node_id=?",(id,));c.commit();c.close()
        _,nodes=self.call('/api/nodes');n=next(n for n in nodes if n['id']==id);self.assertEqual(n['health_status'],'offline');self.assertEqual(n['status'],'offline')
    def test_bootstrap_uses_posix_shell_and_no_ssh_credentials(self):
        code=bootstrap_script('https://panel.example.com','test-token-for-syntax',12)
        subprocess.run(['sh','-n'],input=code.encode(),check=True)
        self.assertNotIn('SSH_PASSWORD',code);self.assertIn('crontab',code)

    def test_remote_uninstall_keeps_record_until_confirmation(self):
        id,token,meta=self.initial_success()
        code,result=self.call(f'/api/nodes/{id}','DELETE');self.assertEqual(code,200,result)
        code,work=self.agent_call('/agent/work',token)
        self.assertEqual(work['job']['operation'],'uninstall')
        rev=work['job']['revision']
        code,script=self.agent_call(f'/agent/job/{rev}.sh',token)
        self.assertEqual(code,200)
        subprocess.run(['sh','-n'],input=script.encode(),check=True)
        self.assertIn(meta['uuid'],script)
        code,nodes=self.call('/api/nodes');self.assertTrue(any(n['id']==id and n['deleting'] for n in nodes))
        code,_=self.agent_call('/agent/report',token,{'state':'online','metadata':meta})
        self.assertEqual(code,200)
        code,result=self.agent_call(f'/agent/uninstalled/{rev}',token,{})
        self.assertEqual(code,200,result)
        code,result=self.agent_call(f'/agent/uninstalled/{rev}',token,{})
        self.assertEqual(code,200,result)
        code,nodes=self.call('/api/nodes');self.assertFalse(any(n['id']==id for n in nodes))
