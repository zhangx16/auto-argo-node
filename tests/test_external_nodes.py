"""Import format handling, export-only ownership, and bounded public URL fetching."""
import base64
import io
import json
import socket
import unittest
from unittest.mock import patch

import yaml
import test_subscriptions as subscription_tests
from app import node_formats as nf, external_nodes as external, import_fetch

UUID = '341cad6d-9bf5-4b09-9c96-a7879ebc8fe1'
VLESS = 'vless://' + UUID + '@edge.example.com:443?security=tls&type=ws&sni=tunnel.example.com&host=tunnel.example.com&path=%2Fws%3Fed%3D2560#External'
SAMPLES = [VLESS,
 'vmess://' + nf.b64encode(json.dumps(dict(v='2', ps='VMess', add='edge.example.com', port='443', id=UUID, aid='0', scy='auto', net='grpc', path='svc', tls='tls', sni='tls.example.com'))),
 'trojan://a%3Ab%40c@edge.example.com:443?sni=tls.example.com#Trojan',
 'ss://' + nf.b64encode('aes-128-gcm:sample-only-password') + '@edge.example.com:8388#SS',
 'hy2://sample-only-password@edge.example.com:443?sni=tls.example.com&obfs=salamander&obfs-password=example#HY2',
 'tuic://' + UUID + ':sample-only-password@edge.example.com:443?sni=tls.example.com&congestion_control=bbr&udp_relay_mode=native#TUIC']


class FormatTests(unittest.TestCase):
    def test_all_protocols_uri_and_config_round_trip(self):
        for link in SAMPLES:
            with self.subTest(scheme=link.split(':', 1)[0]):
                p = nf.parse_uri(link)
                if p['protocol'] == 'vmess':
                    self.assertIsInstance(json.loads(base64.b64decode(nf.to_uri(p)[8:], validate=True)), dict)
                self.assertEqual(nf.parse_uri(nf.to_uri(p)), p)
                for flavor, convert in [('clash', nf.clash_proxy), ('singbox', nf.singbox_outbound)]:
                    parsed = nf.from_config(convert(p), flavor)
                    self.assertEqual(nf.to_uri(parsed), nf.to_uri(p))

    def test_uri_base64_yaml_json_partial_errors_and_no_secrets(self):
        body = '\n'.join(SAMPLES + [SAMPLES[0], 'trojan://secret-never-echo@bad host:443'])
        result = external.parse_content(body)
        self.assertEqual(len(result['nodes']), 6)
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(result['invalid_count'], 1)
        self.assertNotIn('secret-never-echo', json.dumps(result['errors']))
        encoded = base64.b64encode('\n'.join(SAMPLES).encode()).decode()
        self.assertEqual(len(external.parse_content(encoded)['nodes']), 6)
        for flavor, root, convert in [('clash', 'proxies', nf.clash_proxy), ('singbox', 'outbounds', nf.singbox_outbound)]:
            config = {root: [convert(nf.parse_uri(s)) for s in SAMPLES]}
            if flavor == 'singbox':
                config[root] += [{'type': 'selector', 'tag': 'proxy'}, {'type': 'direct', 'tag': 'direct'}]
            self.assertEqual(len(external.parse_content(yaml.safe_dump(config))['nodes']), 6)

    def test_validation_limits_and_unsupported_options(self):
        for bad in ['vless://bad@edge.example.com:443', VLESS.replace('#', '&plugin=bad#'),
                    'ss://' + nf.b64encode('unsupported:password') + '@edge.example.com:443',
                    'tuic://not-uuid:password@edge.example.com:443',
                    VLESS.replace('type=ws', 'type=xhttp')]:
            with self.assertRaises(ValueError):nf.parse_uri(bad)
        with self.assertRaises(ValueError):external.parse_content('x' * (1048576 + 1))
        with self.assertRaises(ValueError):external.parse_content('\n'.join([VLESS] * 1001))
        self.assertEqual(external.parse_content('proxies:\n- name: &a [*a]\n  type: vless\n')['invalid_count'], 1)
        result = external.parse_content('proxies:\n- name: plugin\n  type: ss\n  server: edge.example.com\n  port: 443\n  cipher: aes-128-gcm\n  password: secret-never-echo\n  plugin: v2ray-plugin\n')
        self.assertEqual(result['invalid_count'], 1)
        self.assertNotIn('secret-never-echo', json.dumps(result['errors']))

    def test_legacy_ss_ipv6_and_password_escaping(self):
        p = nf.parse_uri('ss://' + nf.b64encode('aes-128-gcm:a:b@c@[2001:db8::1]:8388') + '#IPv6')
        self.assertEqual(p['server'], '2001:db8::1')
        self.assertEqual(p['password'], 'a:b@c')
        self.assertEqual(nf.parse_uri(nf.to_uri(p)), p)


class FetchTests(unittest.TestCase):
    def addr(self, ip):
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]

    def test_private_reserved_credentials_and_dns_rebinding_blocked(self):
        for ip in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '192.168.1.1', '::1', 'fc00::1', '::ffff:127.0.0.1', '2002:7f00:1::']:
            with patch.object(socket, 'getaddrinfo', return_value=self.addr(ip)):
                with self.assertRaises(ValueError):import_fetch.public_target('https://public.example/sub?token=secret')
        with patch.object(socket, 'getaddrinfo', return_value=self.addr('8.8.8.8') + self.addr('127.0.0.1')):
            with self.assertRaises(ValueError):import_fetch.public_target('https://public.example/sub')
        for url in ['file:///etc/passwd', 'http://user:pass@example.com/sub', 'https://example.com/\nsecret']:
            with self.assertRaises(ValueError):import_fetch.public_target(url)

    def test_connection_uses_pinned_ip_and_tls_hostname(self):
        fake_socket = unittest.mock.Mock()
        with patch.object(socket, 'create_connection', return_value=fake_socket) as create:
            c = import_fetch.PinnedHTTP('public.example', 443, '8.8.8.8', 5)
            c.connect();create.assert_called_once_with(('8.8.8.8', 443), 5)
        ctx = unittest.mock.Mock()
        with patch.object(socket, 'create_connection', return_value=fake_socket), patch.object(import_fetch.ssl, 'create_default_context', return_value=ctx):
            c = import_fetch.PinnedHTTPS('public.example', 443, '8.8.8.8', 5)
            c.connect();ctx.wrap_socket.assert_called_once_with(fake_socket, server_hostname='public.example')

    def test_redirect_revalidation_size_and_gzip_limits(self):
        class Reply:
            status = 200
            def __init__(self, headers=None, content=b'node'):
                self.headers = headers or {};self.stream=io.BytesIO(content)
            def getheader(self,key):return self.headers.get(key)
            def read1(self,size):return self.stream.read(size)
        class Connection:
            sock=None
            def __init__(self,*args):pass
            def request(self,*args,**kw):pass
            def getresponse(self):return response
            def close(self):pass
        response=Reply({'Content-Length':str(1048577)})
        with patch.object(socket,'getaddrinfo',return_value=self.addr('8.8.8.8')), patch.object(import_fetch,'PinnedHTTPS',Connection):
            with self.assertRaises(ValueError):import_fetch.fetch('https://public.example/sub')
            import gzip
            response=Reply({'Content-Encoding':'gzip'},gzip.compress(b'x'*1048577))
            with self.assertRaises(ValueError):import_fetch.fetch('https://public.example/sub')
            response=Reply({'Location':'http://private.example/secret'});response.status=302
            with patch.object(socket,'getaddrinfo',side_effect=[self.addr('8.8.8.8'),self.addr('127.0.0.1')]):
                with self.assertRaises(ValueError):import_fetch.fetch('https://public.example/sub')


class ExternalApiTests(unittest.TestCase):
    setUpClass = classmethod(subscription_tests.SubscriptionTests.setUpClass.__func__)
    tearDownClass = classmethod(subscription_tests.SubscriptionTests.tearDownClass.__func__)
    call = subscription_tests.SubscriptionTests.call
    fetch = subscription_tests.SubscriptionTests.fetch
    create_agent = subscription_tests.SubscriptionTests.create_agent
    enroll_agent = subscription_tests.SubscriptionTests.enroll_agent
    agent_call = subscription_tests.SubscriptionTests.agent_call
    initial_success = subscription_tests.SubscriptionTests.initial_success

    def import_nodes(self):
        code, preview = self.call('/api/external-nodes/preview','POST',{'content':'\n'.join(SAMPLES)})
        self.assertEqual(code,200,preview)
        code, result = self.call('/api/external-nodes/import','POST',{'nodes':preview['nodes'],'source_label':'fixture'})
        self.assertEqual(code,200,result)
        return result['node_ids']

    def test_selective_mixed_export_edit_disable_and_delete(self):
        ids=self.import_nodes();managed,_,_=self.initial_success()
        code,group=self.call('/api/subscriptions','POST',{'name':'Mixed','node_ids':[managed],'external_node_ids':ids})
        self.assertEqual(code,200,group);self.assertEqual(group['node_count'],7)
        for fmt in ('raw','base64','clash','singbox'):
            code,text,_=self.fetch(group['urls'][fmt]);self.assertEqual(code,200)
            if fmt=='clash':self.assertEqual(len(yaml.safe_load(text)['proxies']),7)
            if fmt=='singbox':self.assertEqual(len([n for n in json.loads(text)['outbounds'] if n['type'] not in ('selector','direct')]),7)
        code,nodes=self.call('/api/external-nodes');self.assertEqual(code,200)
        node=next(n for n in nodes if n['id']==ids[0])
        code,_=self.call(f"/api/external-nodes/{node['id']}",'PUT',{'name':'Renamed external','uri':node['uri'],'enabled':True});self.assertEqual(code,200)
        self.assertIn('Renamed%20external',self.fetch(group['urls']['raw'])[1])
        code,_=self.call(f"/api/external-nodes/{node['id']}",'PUT',{'name':'Renamed external','uri':node['uri'],'enabled':False});self.assertEqual(code,200)
        self.assertNotIn('Renamed%20external',self.fetch(group['urls']['raw'])[1])
        self.assertEqual(self.call(f"/api/external-nodes/{node['id']}",'DELETE')[0],200)
        _,groups=self.call('/api/subscriptions');self.assertNotIn(node['id'],next(s for s in groups if s['id']==group['id'])['external_node_ids'])
        _,managed_nodes=self.call('/api/nodes');self.assertIn(managed,[n['id'] for n in managed_nodes])

    def test_auth_duplicates_atomic_import_and_invalid_input(self):
        for path,method,body in [('/api/external-nodes','GET',None),('/api/external-nodes/preview','POST',{'content':VLESS}),('/api/external-nodes/import','POST',{'nodes':[{'uri':VLESS}]})]:
            self.assertEqual(self.call(path,method,body,authed=False)[0],401)
        self.assertEqual(self.call('/api/external-nodes/preview','POST',{'content':VLESS},origin='https://wrong.example')[0],403)
        self.assertEqual(self.call('/api/external-nodes/preview','POST',{'url':'http://127.0.0.1/secret'})[0],422)
        self.assertEqual(self.call('/api/external-nodes/preview','POST',{'content':VLESS,'url':'https://example.com'})[0],422)
        ids=self.import_nodes()
        code,duplicate=self.call('/api/external-nodes/import','POST',{'nodes':[{'uri':VLESS,'name':'different name'}]})
        self.assertEqual(code,200);self.assertEqual(duplicate['added'],0);self.assertEqual(duplicate['duplicates'],1)
        _,before=self.call('/api/external-nodes')
        code,_=self.call('/api/external-nodes/import','POST',{'nodes':[{'uri':VLESS.replace('443','8443')},{'uri':'invalid'}]})
        self.assertEqual(code,422)
        _,after=self.call('/api/external-nodes');self.assertEqual(len(before),len(after))
        code,_=self.call('/api/subscriptions','POST',{'name':'Bad selection','external_node_ids':[999999]});self.assertEqual(code,404)
