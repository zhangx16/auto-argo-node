"""Access control, selective membership, live rendering and format compatibility."""
import base64
import json
import unittest
import urllib.request
import urllib.error
from urllib.parse import urlsplit

import yaml
import test_api as api_tests
import test_agent as agent_tests


class SubscriptionTests(unittest.TestCase):
    setUpClass = classmethod(api_tests.ApiTests.setUpClass.__func__)
    tearDownClass = classmethod(api_tests.ApiTests.tearDownClass.__func__)
    call = api_tests.ApiTests.call
    create_agent = agent_tests.AgentApiTests.create_agent
    enroll_agent = agent_tests.AgentApiTests.enroll_agent
    agent_call = agent_tests.AgentApiTests.agent_call
    initial_success = agent_tests.AgentApiTests.initial_success

    def new_sub(self, ids, **changes):
        body = dict(name='我的订阅', node_ids=ids)
        body.update(changes)
        code, data = self.call('/api/subscriptions', 'POST', body)
        self.assertEqual(code, 200, data)
        return data

    def fetch(self, url, authed=False):
        u = urlsplit(url)
        path = u.path + ('?' + u.query if u.query else '')
        headers = {'Cookie': 'sbmgmt=' + self.cookie} if authed else {}
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def test_selection_all_formats_and_live_domain(self):
        selected, token, meta = self.initial_success()
        other, _, _ = self.initial_success()
        sub = self.new_sub([selected])
        self.assertEqual(sub['node_ids'], [selected])
        _, raw, headers = self.fetch(sub['urls']['raw'])
        self.assertEqual(len(raw.strip().splitlines()), 1)
        self.assertIn(meta['uuid'], raw)
        _, nodes = self.call('/api/nodes')
        excluded = next(n for n in nodes if n['id'] == other)
        self.assertNotIn(excluded['uuid'], raw)
        _, encoded, _ = self.fetch(sub['urls']['base64'])
        self.assertEqual(base64.b64decode(encoded).decode(), raw)
        _, clash, _ = self.fetch(sub['urls']['clash'])
        p = yaml.safe_load(clash)['proxies'][0]
        self.assertEqual(p['server'], 'edge.example.com')
        self.assertEqual(p['servername'], meta['domain'])
        self.assertEqual(p['ws-opts']['headers']['Host'], meta['domain'])
        self.assertEqual(p['ws-opts']['path'], meta['path'])
        self.assertEqual(p['ws-opts']['max-early-data'], 2560)
        self.assertFalse(p['skip-cert-verify'])
        _, config, _ = self.fetch(sub['urls']['singbox'])
        decoded = json.loads(config)
        self.assertEqual(decoded['dns']['servers'][0]['type'], 'https')
        self.assertEqual(decoded['route']['default_domain_resolver']['server'], 'dns-direct')
        p = next(p for p in decoded['outbounds'] if p['type'] == 'vless')
        self.assertEqual(p['transport']['path'], meta['path'])
        self.assertEqual(p['transport']['early_data_header_name'], 'Sec-WebSocket-Protocol')
        self.assertEqual(headers['cache-control'], 'no-store')
        meta['domain'] = 'updated-sub.trycloudflare.com'
        code, _ = self.agent_call('/agent/report', token, dict(state='online', metadata=meta, local_ws=True, public_ws=True))
        self.assertEqual(code, 200)
        _, updated, _ = self.fetch(sub['urls']['clash'])
        self.assertIn(meta['domain'], updated)
        self.assertNotIn('agent-qa.trycloudflare.com', updated)

    def test_token_scope_rotation_disable_expiry_delete(self):
        node, _, _ = self.initial_success()
        sub = self.new_sub([node])
        other = self.new_sub([node])
        wrong = sub['urls']['raw'].replace('/subscribe/' + str(sub['id']) + '/', '/subscribe/' + str(other['id']) + '/')
        self.assertEqual(self.fetch(wrong)[0], 404)
        code, rotated = self.call(f"/api/subscriptions/{sub['id']}/rotate", 'POST')
        self.assertEqual(code, 200)
        self.assertEqual(self.fetch(sub['urls']['raw'])[0], 404)
        self.assertEqual(self.fetch(rotated['urls']['raw'])[0], 200)
        code, _ = self.call(f"/api/subscriptions/{sub['id']}", 'PUT', dict(name='已停用', node_ids=[node], enabled=False))
        self.assertEqual(code, 200)
        self.assertEqual(self.fetch(rotated['urls']['raw'])[0], 404)
        expired = self.new_sub([node], expires_at='2000-01-01T00:00:00Z')
        self.assertTrue(expired['expired'])
        self.assertEqual(self.fetch(expired['urls']['raw'])[0], 404)
        code, _ = self.call(f"/api/subscriptions/{other['id']}", 'DELETE')
        self.assertEqual(code, 200)
        self.assertEqual(self.fetch(other['urls']['raw'])[0], 404)
        _, nodes = self.call('/api/nodes')
        self.assertIn(node, [n['id'] for n in nodes])

    def test_owner_auth_origin_validation_and_atomic_update(self):
        node, _, _ = self.initial_success()
        sub = self.new_sub([node])
        for suffix in ('', f"/{sub['id']}/preview", f"/{sub['id']}/export"):
            self.assertEqual(self.call('/api/subscriptions' + suffix, authed=False)[0], 401)
        self.assertEqual(self.call('/api/subscriptions', 'POST', dict(name='test'), origin='https://wrong.example')[0], 403)
        for body in (dict(name=' '), dict(name='x\ny'), dict(name='x', node_ids=[node, node]), dict(name='x', node_ids=[-1]), dict(name='x', expires_at='2027-01-01T00:00')):
            self.assertEqual(self.call('/api/subscriptions', 'POST', body)[0], 422)
        self.assertEqual(self.call(f"/api/subscriptions/{sub['id']}", 'PUT', dict(name='bad', node_ids=[999999]))[0], 409)
        _, groups = self.call('/api/subscriptions')
        after = next(s for s in groups if s['id'] == sub['id'])
        self.assertEqual(after['node_ids'], [node])
        self.assertEqual(after['name'], sub['name'])
        self.assertEqual(self.fetch(sub['urls']['raw'].replace('format=raw', 'format=bad'))[0], 422)
        empty = self.new_sub([])
        self.assertEqual(self.fetch(empty['urls']['raw'])[0], 409)

    def test_uninstall_exclusion_and_cascade(self):
        first, token, _ = self.initial_success()
        second, _, _ = self.initial_success()
        sub = self.new_sub([first, second])
        self.assertEqual(self.call(f'/api/nodes/{first}', 'DELETE')[0], 200)
        _, raw, _ = self.fetch(sub['urls']['raw'])
        self.assertEqual(len(raw.strip().splitlines()), 1)
        _, work = self.agent_call('/agent/work', token)
        rev = work['job']['revision']
        self.agent_call(f'/agent/job/{rev}.sh', token)
        self.assertEqual(self.agent_call(f'/agent/uninstalled/{rev}', token, {})[0], 200)
        _, groups = self.call('/api/subscriptions')
        self.assertEqual(next(s for s in groups if s['id'] == sub['id'])['node_ids'], [second])

    def test_safe_unique_names_and_owner_download(self):
        first, _, _ = self.initial_success()
        second, _, _ = self.initial_success()
        for node in (first, second):
            self.assertEqual(self.call(f'/api/agent-nodes/{node}', 'PATCH', dict(remark='DIRECT', node_port=18080, client_address='', client_port=443))[0], 200)
        sub = self.new_sub([second, first])
        _, output, _ = self.fetch(sub['urls']['clash'])
        config = yaml.safe_load(output)
        names = [p['name'] for p in config['proxies']]
        self.assertEqual(len(set(names)), 2)
        self.assertNotIn('DIRECT', names)
        self.assertTrue(names[0].endswith('-cf'))
        self.assertIn(str(first), names[1])
        code, preview = self.call(f"/api/subscriptions/{sub['id']}/preview?format=singbox")
        self.assertEqual(code, 200)
        self.assertEqual(preview['node_count'], 2)
        code, output, headers = self.fetch(f"/api/subscriptions/{sub['id']}/export?format=singbox", authed=True)
        self.assertEqual(code, 200)
        self.assertIn('attachment;', headers['content-disposition'])
        self.assertEqual(json.loads(output), json.loads(preview['content']))
