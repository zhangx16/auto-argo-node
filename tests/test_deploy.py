import os
import tempfile
os.environ['SINGBOX_MGMT_DATA'] = tempfile.mkdtemp(prefix='sb-deploy-tests-')
import base64
import io
import json
import socket
import subprocess
import threading
import time
import unittest
from urllib.parse import urlparse, parse_qs
import paramiko
from pydantic import ValidationError
from app import deploy, db

HOST_KEY=paramiko.RSAKey.generate(2048)
PASSWORD='only-in-memory-test-secret'
META={'domain':'sample-test.trycloudflare.com','uuid':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','path':'/sample','local_port':18080}

class Server(paramiko.ServerInterface):
    def __init__(self, allow=True, methods='password,publickey'):
        self.methods=methods
        self.executed=threading.Event(); self.allow=allow; self.authed=False
    def check_auth_password(self,u,p):
        self.authed=True
        return paramiko.AUTH_SUCCESSFUL if self.allow and 'password' in self.methods and u=='root' and p==PASSWORD else paramiko.AUTH_FAILED
    def check_auth_publickey(self,u,k):
        self.authed=True
        return paramiko.AUTH_SUCCESSFUL
    def check_auth_interactive(self,u,submethods):
        return paramiko.InteractiveQuery('SSH login','',('Password:',False))
    def check_auth_interactive_response(self,responses):
        return paramiko.AUTH_SUCCESSFUL if self.allow and responses==[PASSWORD] else paramiko.AUTH_FAILED
    def get_allowed_auths(self,u): return self.methods
    def check_channel_request(self,kind,id): return paramiko.OPEN_SUCCEEDED
    def check_channel_exec_request(self,channel,command):
        self.executed.set();return True

class Peer:
    def __init__(self,allow=True,delay=0,methods='password,publickey'):
        self.delay=delay
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.sock.listen(1)
        self.port=self.sock.getsockname()[1];self.server=Server(allow,methods);self.payload=b''
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        try:
            connection,_=self.sock.accept();t=paramiko.Transport(connection);t.add_server_key(HOST_KEY);t.start_server(server=self.server)
            channel=t.accept(8)
            if channel and self.server.executed.wait(8):
                while True:
                    part=channel.recv(32768)
                    if not part:break
                    self.payload+=part
                time.sleep(self.delay)
                channel.sendall(b'test log\nSB_WEB_')
                channel.send_stderr(b'stderr warning\n')
                time.sleep(.1)
                channel.sendall(('RESULT='+json.dumps(META)+'\n').encode())
                channel.send_exit_status(0);channel.shutdown_write()
                time.sleep(.3)
            t.close()
        except (EOFError,paramiko.SSHException): pass
        finally:self.sock.close()

class DeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):db.init_db();deploy.init()
    def body(self,port=22,**kw):
        values=dict(host='127.0.0.1',ssh_port=port,host_key=HOST_KEY.get_base64(),password=PASSWORD,remark="测试 ' $(touch /tmp/never-execute) 节点")
        values.update(kw)
        return deploy.DeployBody(**values)
    def run_worker(self,peer,body):
        n=db.create_node(body.remark,body.node_port,'',body.client_address,base64.b64encode(os.urandom(24)).decode());db.update_node(n['id'],status='installing')
        self.assertTrue(deploy.SLOTS.acquire(False));deploy.worker(n['id'],body);peer.thread.join(10)
        return db.get_node(n['id'])
    def test_model_rejects_bad_config(self):
        for changes in ({'host':'--bad'},{'client_address':'domain/path'},{'client_port':22},{'ws_path':'/bad\n1'},{'uuid':'bad'},{'node_port':80}):
            with self.assertRaises(ValidationError):self.body(**changes)
    def test_both_drivers_shell_syntax_and_secret_exclusion(self):
        body=self.body();script=deploy.driver(body)
        self.assertNotIn(PASSWORD,script)
        subprocess.run(['sh','-n'],input=script.encode(),check=True)
        inner=script.split("<<'SB_DRIVER'\n",1)[1].split('\nSB_DRIVER',1)[0]
        subprocess.run(['bash','-n'],input=inner.encode(),check=True)
        self.assertIn('web-library',inner)
        subprocess.run(['sh','-n'],input=deploy.driver(self.body(operation='read')).encode(),check=True)
    def test_link_keeps_host_and_sni_and_early_data(self):
        link=deploy.make_link(META,self.body())
        parts=urlparse(link);q=parse_qs(parts.query)
        self.assertEqual(parts.hostname,'hk.031216.xyz');self.assertEqual(q['host'],[META['domain']]);self.assertEqual(q['sni'],[META['domain']]);self.assertEqual(q['path'],['/sample?ed=2560'])
    def test_probe_real_ssh_transport(self):
        peer=Peer();result=deploy.probe(deploy.Target(host='127.0.0.1',ssh_port=peer.port));self.assertEqual(result['host_key'],HOST_KEY.get_base64());peer.thread.join(10)
    def test_worker_password_real_ssh(self):
        peer=Peer();body=self.body(peer.port);node=self.run_worker(peer,body)
        self.assertEqual(node['status'],'online',node['last_error']);self.assertTrue(node['share_link'].startswith('vless://'));self.assertNotIn(PASSWORD,peer.payload.decode());self.assertEqual(body.password.get_secret_value(),'');self.assertNotIn(PASSWORD,str(db.list_logs(node['id'])))
    def test_worker_key_real_ssh_read(self):
        key=paramiko.RSAKey.generate(2048);stream=io.StringIO();key.write_private_key(stream)
        peer=Peer();body=self.body(peer.port,password='',private_key=stream.getvalue(),operation='read');node=self.run_worker(peer,body)
        self.assertEqual(node['status'],'online',node['last_error']);self.assertNotIn('PRIVATE KEY',peer.payload.decode());self.assertNotIn('SB_PAYLOAD',peer.payload.decode())
    def test_keyboard_interactive_password_login(self):
        peer=Peer(methods='publickey,keyboard-interactive');node=self.run_worker(peer,self.body(peer.port))
        self.assertEqual(node['status'],'online',node['last_error'])
    def test_host_key_mismatch_before_authentication(self):
        peer=Peer();body=self.body(peer.port,host_key=paramiko.RSAKey.generate(2048).get_base64());node=self.run_worker(peer,body)
        self.assertEqual(node['status'],'error');self.assertFalse(peer.server.authed);self.assertIn('密钥已变化',node['last_error'])
    def test_failed_auth_is_reported_without_secret(self):
        peer=Peer(allow=False);node=self.run_worker(peer,self.body(peer.port));self.assertEqual(node['status'],'error');self.assertNotIn(PASSWORD,node['last_error']);self.assertIn('认证失败',node['last_error'])
    def test_interactive_failure_reports_server_methods(self):
        peer=Peer(allow=False,methods='publickey,keyboard-interactive');node=self.run_worker(peer,self.body(peer.port))
        self.assertEqual(node['status'],'error');self.assertIn('交互认证未通过',node['last_error']);self.assertNotIn(PASSWORD,node['last_error'])
    def test_latest_logs_are_returned(self):
        n=db.create_node('logs',18081,'','','logtest')
        for x in range(205):db.add_log(n['id'],str(x))
        logs=db.list_logs(n['id']);self.assertEqual(logs[-1]['line'],'204');self.assertEqual(logs[0]['line'],'5')


class NodeEditTests(unittest.TestCase):
    def setUp(self):
        db.init_db();deploy.init()
        self.node=db.create_node('Original',18080,META['domain'],'hk.031216.xyz',os.urandom(18).hex())
        c=db.get_conn();c.execute('INSERT INTO deployments VALUES (?,?,?,?,?,?)',(self.node['id'],'127.0.0.1',22,443,META['path'],'test-fingerprint'));c.commit()
        body=deploy.DeployBody(host='127.0.0.1',host_key=HOST_KEY.get_base64(),remark='Original')
        db.update_node(self.node['id'],status='online',uuid=META['uuid'],share_link=deploy.make_link(META,body))
    def test_edit_regenerates_link_and_keeps_tls_host(self):
        result=deploy.save_settings(self.node['id'],deploy.NodeEditBody(remark='New name',node_port=18080,client_address='1.1.1.1',client_port=8443))
        link=urlparse(result['share_link']);params=parse_qs(link.query)
        self.assertEqual(link.hostname,'1.1.1.1');self.assertEqual(link.port,8443)
        self.assertEqual(params['sni'],[META['domain']]);self.assertEqual(params['host'],[META['domain']]);self.assertEqual(result['remark'],'New name');self.assertEqual(result['client_port'],8443)
    def test_listener_change_requires_remote_auth(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as e:deploy.save_settings(self.node['id'],deploy.NodeEditBody(remark='X',node_port=18081))
        self.assertEqual(e.exception.status_code,409);self.assertEqual(db.get_node(self.node['id'])['node_port'],18080)
    def test_running_task_cannot_be_edited(self):
        from fastapi import HTTPException
        db.update_node(self.node['id'],status='installing')
        with self.assertRaises(HTTPException) as e:deploy.save_settings(self.node['id'],deploy.NodeEditBody(remark='X',node_port=18080))
        self.assertEqual(e.exception.status_code,409)
    def test_update_shell_and_pinned_identity(self):
        body=deploy.DeployBody(host='127.0.0.1',host_key=HOST_KEY.get_base64(),operation='update',old_port=18080,node_port=18081,expected_uuid=META['uuid'])
        script=deploy.driver(body);subprocess.run(['sh','-n'],input=script.encode(),check=True)
        inner=script.split("<<'SB_DRIVER'\n",1)[1].split('\nSB_DRIVER',1)[0];subprocess.run(['bash','-n'],input=inner.encode(),check=True)
        helper=script.split("<<'SB_PORT_HELPER'\n",1)[1].split('\nSB_PORT_HELPER',1)[0];subprocess.run(['bash','-n'],input=helper.encode(),check=True)

if __name__=='__main__':unittest.main(verbosity=2)
