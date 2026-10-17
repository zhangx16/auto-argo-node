"""Exercise the public HTTP contracts with a temporary app and SSH peer."""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from itsdangerous import TimestampSigner
from test_deploy import Peer, HOST_KEY, PASSWORD, META

class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        sock=socket.socket();sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1];sock.close()
        cls.base=f'http://127.0.0.1:{cls.port}'
        env=os.environ|{'SINGBOX_MGMT_DATA':cls.tmp.name}
        cls.process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(cls.port)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                urllib.request.urlopen(cls.base+'/health',timeout=.2).close();break
            except Exception:time.sleep(.05)
        else:raise RuntimeError('test API failed to start')
        secret=Path(cls.tmp.name,'secret.key').read_text().strip()
        cls.cookie=TimestampSigner(secret).sign(base64.b64encode(json.dumps({'user':'qa','authed':True}).encode())).decode()
    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.wait(timeout=10);cls.tmp.cleanup()
    def call(self,path,method='GET',body=None,origin=None,authed=True):
        headers={'Content-Type':'application/json'}
        if authed:headers['Cookie']='sbmgmt='+self.cookie
        if origin:headers['Origin']=origin
        req=urllib.request.Request(self.base+path,method=method,headers=headers,data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req,timeout=10) as r:return r.status,json.load(r)
        except urllib.error.HTTPError as e:return e.code,json.load(e)
    def create(self,delay=0):
        peer=Peer(delay=delay)
        body={'host':'127.0.0.1','ssh_port':peer.port,'password':PASSWORD,'host_key':HOST_KEY.get_base64(),'remark':'HTTP QA','node_port':18080}
        code,result=self.call('/api/deploy','POST',body)
        self.assertEqual(code,200,result)
        return result['id'],body,peer
    def wait_node(self,id,state='online'):
        for _ in range(100):
            _,nodes=self.call('/api/nodes');n=next(n for n in nodes if n['id']==id)
            if n['status']==state:return n
            time.sleep(.05)
        self.fail(f'node did not reach {state}: {n["status"]}')
    def test_auth_origin_and_validation(self):
        code,_=self.call('/api/nodes',authed=False);self.assertEqual(code,401)
        code,_=self.call('/api/deployments/1','PATCH',{'remark':'X','node_port':18080},origin='https://wrong.example');self.assertEqual(code,403)
        code,result=self.call('/api/deploy','POST',{'host':'127.0.0.1','host_key':HOST_KEY.get_base64(),'password':PASSWORD,'node_port':80});self.assertEqual(code,422);self.assertNotIn(PASSWORD,json.dumps(result))
    def test_edit_link_listener_guard_and_delete(self):
        id,_,peer=self.create();self.wait_node(id);peer.thread.join(10)
        code,node=self.call(f'/api/deployments/{id}','PATCH',{'remark':'Edited','node_port':18080,'client_address':'new.example.com','client_port':8443})
        self.assertEqual(code,200,node);self.assertIn('@new.example.com:8443?',node['share_link']);self.assertIn('sni='+META['domain'],node['share_link'])
        code,_=self.call(f'/api/deployments/{id}','PATCH',{'remark':'Edited','node_port':18081,'client_address':'new.example.com','client_port':8443});self.assertEqual(code,409)
        code,_=self.call(f'/api/nodes/{id}','DELETE');self.assertEqual(code,409)
        _,nodes=self.call('/api/nodes');self.assertIn(id,[n['id'] for n in nodes])
    def test_failed_remote_update_keeps_record_and_previous_link(self):
        id,body,peer=self.create();before=self.wait_node(id);peer.thread.join(10)
        _,nodes=self.call('/api/nodes');count=len(nodes)
        body.update(operation='update',node_port=18081,remark='Try edit')
        code,result=self.call(f'/api/deploy/{id}','POST',body);self.assertEqual(code,200,result);self.assertEqual(result['id'],id)
        after=self.wait_node(id,'error')
        self.assertEqual(after['node_port'],18080);self.assertEqual(after['share_link'],before['share_link']);self.assertEqual(after['remark'],before['remark'])
        _,nodes=self.call('/api/nodes');self.assertEqual(len(nodes),count)
    def test_running_job_cannot_be_deleted(self):
        id,_,peer=self.create(delay=.6)
        code,_=self.call(f'/api/nodes/{id}','DELETE');self.assertEqual(code,409)
        self.wait_node(id);peer.thread.join(10)
        code,_=self.call(f'/api/nodes/{id}','DELETE');self.assertEqual(code,409)
