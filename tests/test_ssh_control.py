import base64,json,time,unittest
from unittest.mock import patch,MagicMock
from fastapi import FastAPI,HTTPException
from app import agent,db,ssh_control as ssh
import test_reliability

class SSHControlTests(unittest.TestCase):
    setUp=test_reliability.ReliabilityTests.setUp
    tearDown=test_reliability.ReliabilityTests.tearDown
    def host(self,mode='agent',credential=True):
        c=db.get_conn();sid='ssh-fixture'
        c.execute('INSERT INTO ssh_servers(id,name,host,host_key,credentials,mode,created_at) VALUES (?,?,?,?,?,?,?)',
          (sid,'SSH QA','192.0.2.1',base64.b64encode(b'fixture-host-public-key').decode(),ssh.seal({'password':'private-fixture-password'}) if credential else '',mode,db.utcnow()))
        c.execute('INSERT INTO ssh_nodes VALUES (?,?)',(self.id,sid));c.commit();return sid
    def test_credentials_and_script_are_encrypted_and_not_returned(self):
        sid=self.host();s=ssh.server(sid)
        self.assertNotIn('private-fixture-password',s['credentials']);self.assertEqual(ssh.unseal(s['credentials'])['password'],'private-fixture-password')
        visible=ssh.public(s);self.assertNotIn('credentials',visible);self.assertNotIn('private-fixture-password',json.dumps(visible))
        task=ssh.queue(sid,'custom',payload={'script':'echo private-script-fixture'})
        payload=db.get_conn().execute('SELECT payload FROM ssh_tasks WHERE id=?',(task['id'],)).fetchone()[0]
        self.assertNotIn('private-script-fixture',payload)
        with self.assertRaises(HTTPException):ssh.queue(sid,'diagnose')
    def test_missing_credentials_requires_panel_input(self):
        sid=self.host(credential=False)
        with self.assertRaises(HTTPException) as error:ssh.queue(sid,'diagnose')
        self.assertIn('凭据',error.exception.detail)
    def test_handoff_excludes_concurrent_configuration(self):
        sid=self.host();ssh.queue(sid,'switch_ssh')
        with self.assertRaises(HTTPException):agent.enqueue(self.id,'read',self.config)
    def test_ssh_mode_blocks_old_polling_and_repair(self):
        sid=self.host('ssh');token='fixture-runtime'
        db.get_conn().execute('UPDATE node_agents SET token_hash=? WHERE node_id=?',(agent.digest(token),self.id));db.get_conn().commit()
        rev=agent.enqueue(self.id,'read',self.config)
        app=FastAPI();agent.register(app,lambda request:'fixture',lambda:'https://fixture.invalid')
        endpoints={r.path:r.endpoint for r in app.routes}
        self.assertIsNone(endpoints['/agent/work'](token)['job'])
        self.assertFalse(endpoints['/agent/work'](token)['control']['repair_allowed'])
        self.assertIsNotNone(endpoints['/agent/work'](token,'1')['job'])
        with self.assertRaises(HTTPException):endpoints['/agent/job/{revision}.sh'](rev,token)
        self.assertFalse(endpoints['/agent/repair-permit'](token)['allowed'])
    def test_unknown_journal_never_replays_command(self):
        sid=self.host();task=ssh.queue(sid,'custom',payload={'script':'echo should-not-run'})
        db.get_conn().execute('UPDATE ssh_tasks SET started=1 WHERE id=?',(task['id'],));db.get_conn().commit()
        with patch.object(ssh,'connect',return_value=MagicMock()),patch.object(ssh.paramiko.SFTPClient,'from_transport',return_value=MagicMock()),patch.object(ssh,'exists',return_value=False),patch.object(ssh,'command',return_value=(0,'')) as run,patch.object(ssh,'write') as write:
            ssh.work(task['id']);write.assert_not_called()
            self.assertFalse(any('nohup' in str(call) for call in run.call_args_list))
        r=db.get_conn().execute('SELECT status FROM ssh_tasks WHERE id=?',(task['id'],)).fetchone();self.assertEqual(r['status'],'unknown')
    def test_redaction_removes_links_and_auth_lines(self):
        output=ssh.redact('ok\nAuthorization: Bearer private\npassword=fixture\nvless://secret@host\nhttps://host/private\ncomplete')
        for secret in ('Bearer private','password=fixture','vless://secret','https://host/private'):self.assertNotIn(secret,output)
        self.assertIn('complete',output)
    def test_first_host_sync_does_not_override_manual_bindings(self):
        c=db.get_conn();c.execute("INSERT INTO monitor_hosts(id,name,hub_id,hub_token,token_hash,token_secret) VALUES ('h','Host',1,'fixture','fixture','fixture')")
        c.execute('INSERT INTO monitor_nodes VALUES (?,?)',(self.id,'h'));c.commit()
        ssh.sync_hosts();self.assertIsNotNone(ssh.binding(self.id))
        c.execute('DELETE FROM ssh_nodes WHERE node_id=?',(self.id,));c.commit();ssh.sync_hosts();self.assertIsNone(ssh.binding(self.id))

    def test_completed_uninstall_can_be_collected_after_node_is_deleted(self):
        sid=self.host();task=ssh.queue(sid,'agent_tick',self.id)
        db.get_conn().execute('UPDATE ssh_tasks SET started=1 WHERE id=?',(task['id'],));db.get_conn().commit();db.delete_node(self.id)
        with patch.object(ssh,'connect',return_value=MagicMock()),patch.object(ssh.paramiko.SFTPClient,'from_transport',return_value=MagicMock()),patch.object(ssh,'exists',side_effect=lambda ftp,path:path.endswith(('result.rc','output.log'))),patch.object(ssh,'read',side_effect=lambda ftp,path,limit:'0' if path.endswith('result.rc') else 'uninstalled'),patch.object(ssh,'command',return_value=(0,'')),patch.object(ssh,'verify_nodes') as verify:
            ssh.work(task['id']);verify.assert_not_called()
        self.assertEqual(db.get_conn().execute('SELECT status FROM ssh_tasks WHERE id=?',(task['id'],)).fetchone()[0],'done')

    def test_relay_requires_live_other_node_and_never_falls_back_to_direct(self):
        sid=self.host()
        with self.assertRaises(HTTPException):ssh.relay_node(self.id,sid)
        with self.assertRaises(ssh.RelayUnavailable):ssh.relay_node(99999)
        with patch.object(ssh.deploy,'transport') as direct:
            with self.assertRaises(ssh.RelayUnavailable):ssh.open_transport({'host':'192.0.2.1','port':22,'relay_node_id':99999})
            direct.assert_not_called()

    def test_fixed_reinstall_uses_full_installer_even_with_existing_link(self):
        from app import fixed_jobs,cloudflare
        a=agent.row(self.id);a['agent_version']=8
        config=self.config|{'tunnel_mode':'fixed','fixed_hostname':'fixture.example.com'}
        with patch.object(cloudflare,'provision'),patch.object(fixed_jobs,'install',return_value='full installer') as install,patch.object(fixed_jobs,'migrate') as migrate:
            self.assertEqual(agent.job_script(a,{'config_json':json.dumps(config),'operation':'install'}),'full installer')
            install.assert_called_once();migrate.assert_not_called()

    def test_relay_tunnel_pins_host_and_cleans_up_after_auth_failure(self):
        from app import proxy_runtime
        context=MagicMock();context.__enter__.return_value=18090
        sock=MagicMock();sock.recv.side_effect=[bytes([b]) for b in b'HTTP/1.1 200 Connection established\r\n\r\n']
        t=MagicMock();t.get_remote_server_key.return_value.get_base64.return_value='unexpected-host'
        with patch.object(proxy_runtime,'runtime',return_value=context),patch.object(ssh.socket,'create_connection',return_value=sock),patch.object(ssh.paramiko,'Transport',return_value=t),patch.object(ssh.deploy,'transport') as direct:
            with self.assertRaises(ssh.HostChanged):ssh.connect({'host':'2001:db8::1','port':22,'relay_node_id':self.id,'host_key':'expected-host'})
            direct.assert_not_called();t.auth_password.assert_not_called();t.close.assert_called_once();context.__exit__.assert_called_once()
            self.assertIn(b'CONNECT [2001:db8::1]:22 ',sock.sendall.call_args.args[0])

    def test_failed_connect_response_closes_proxy_and_socket(self):
        from app import proxy_runtime
        context=MagicMock();context.__enter__.return_value=18090
        sock=MagicMock();sock.recv.side_effect=[bytes([b]) for b in b'HTTP/1.1 502 Bad Gateway\r\n\r\n']
        with patch.object(proxy_runtime,'runtime',return_value=context),patch.object(ssh.socket,'create_connection',return_value=sock),patch.object(ssh.paramiko,'Transport') as transport:
            with self.assertRaises(ssh.RelayUnavailable):ssh.open_transport({'host':'192.0.2.1','port':22,'relay_node_id':self.id})
            transport.assert_not_called();sock.close.assert_called_once();context.__exit__.assert_called_once()

    def test_unknown_task_cannot_change_relay_and_bindings_prevent_self_relay(self):
        sid=self.host();task=ssh.queue(sid,'diagnose')
        db.get_conn().execute("UPDATE ssh_tasks SET status='unknown' WHERE id=?",(task['id'],));db.get_conn().commit()
        other=db.create_node('Relay QA',18081,'relay.example.com','','relay-fixture')['id']
        db.update_node(other,status='online',share_link=self.link)
        app=FastAPI();ssh.register(app,lambda req:'owner');routes={r.path:r.endpoint for r in app.routes}
        body=ssh.ConnectionBody(name='SSH QA',host='192.0.2.1',host_key=ssh.server(sid)['host_key'],relay_node_id=other)
        with patch.object(ssh,'connect') as connect:
            with self.assertRaises(HTTPException):routes['/api/ssh/servers/{server_id}'](sid,body,None)
            connect.assert_not_called()
        db.get_conn().execute("UPDATE ssh_tasks SET status='done' WHERE id=?",(task['id'],))
        db.get_conn().execute('UPDATE ssh_servers SET relay_node_id=? WHERE id=?',(other,sid));db.get_conn().commit()
        with self.assertRaises(HTTPException):routes['/api/ssh/servers/{server_id}/nodes'](sid,ssh.BindingBody(node_ids=[other]),None)
