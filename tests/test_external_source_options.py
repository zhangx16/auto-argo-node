import json,unittest
from unittest.mock import patch
from pydantic import ValidationError
from app import sources,subscriptions,external_nodes,db
import test_reliability

V6='trojan://fixture-password@[2606:4700::1111]:443?sni=tls.example.com#%E3%80%90%E6%B5%8B%E8%AF%95%E3%80%91%E3%80%90%E4%BC%98%E5%8C%96%E3%80%91HK%E3%80%901x%E3%80%91'
V4='trojan://fixture-password@8.8.8.8:443?sni=tls.example.com#%E3%80%90%E7%9B%B4%E8%BF%9E%E3%80%91Other'
class SourceOptionsTests(unittest.TestCase):
    def setUp(self):test_reliability.ReliabilityTests.setUp(self);subscriptions.init()
    tearDown=test_reliability.ReliabilityTests.tearDown
    def source(self,**fields):
        with patch.object(sources.import_fetch,'public_target'):
            return sources.save(sources.SourceBody(name='Upstream',url='https://example.com/private-fixture',**fields))
    def test_transform_keeps_tls_auth_port_and_suffix_with_verified_mapping(self):
        s=self.source(clean_prefixes=True,address_overrides={'2606:4700::1111':'1.1.1.1'})
        result=sources.transform(sources.get(s['id']),external_nodes.parse_content(V6))
        p=external_nodes.parse_uri(result['nodes'][0]['uri']);self.assertEqual(p['server'],'1.1.1.1');self.assertEqual(p['sni'],'tls.example.com');self.assertEqual(p['password'],'fixture-password');self.assertEqual(p['port'],443);self.assertEqual(p['name'],'HK【1x】')
        self.assertEqual(result['transforms'],{'renamed':1,'ipv4_replaced':1,'ipv6_filtered':0})
    def test_ws_host_is_preserved_when_replacing_only_the_connection_address(self):
        uri=V6.replace('?sni=tls.example.com','?sni=tls.example.com&type=ws&host=2606%3A4700%3A%3A1111')
        source=self.source(address_overrides={'2606:4700::1111':'1.1.1.1'})
        result=sources.transform(sources.get(source['id']),external_nodes.parse_content(uri))
        p=external_nodes.parse_uri(result['nodes'][0]['uri']);self.assertEqual(p['server'],'1.1.1.1');self.assertEqual(p['host'],'2606:4700::1111');self.assertEqual(p['sni'],'tls.example.com')

    def test_ipv6_filter_is_explicit_and_does_not_guess_addresses(self):
        s=self.source(clean_prefixes=True,ipv4_only=True)
        result=sources.transform(sources.get(s['id']),external_nodes.parse_content(V6+'\n'+V4));self.assertEqual(len(result['nodes']),1);self.assertEqual(result['transforms']['ipv6_filtered'],1)
        with self.assertRaises(ValueError):sources.transform(sources.get(s['id']),external_nodes.parse_content(V6))
        for mapping in [{'2606:4700::1111':'127.0.0.1'},{'8.8.8.8':'1.1.1.1'}]:
            with self.assertRaises(ValidationError):sources.SourceBody(name='x',address_overrides=mapping)
    def test_refresh_preserves_ids_and_source_account_usage_is_counted_once(self):
        target=subscriptions.save(subscriptions.SubscriptionBody(name='Upstream only'))
        s=self.source(subscription_ids=[target['id']],clean_prefixes=True,address_overrides={'2606:4700::1111':'1.1.1.1'})
        parsed=external_nodes.parse_content(V6+'\n'+V4);parsed['_usage']={'upload':123,'download':456,'total':10000,'expire':1900000000}
        sources.apply(s['id'],s['revision'],parsed);ids=[r['id'] for r in subscriptions.external_members(target['id'])]
        sources.apply(s['id'],s['revision'],parsed);self.assertEqual([r['id'] for r in subscriptions.external_members(target['id'])],ids)
        usage=subscriptions.usage(target);self.assertEqual(usage['scope'],'upstream-account');self.assertEqual(usage['used'],579);self.assertEqual(usage['total'],10000);self.assertEqual(usage['external_measured_count'],2)
        header=subscriptions.usage_headers(target)['Subscription-Userinfo'];self.assertIn('upload=123',header);self.assertIn('expire=1900000000',header)
        subset=subscriptions.save(subscriptions.SubscriptionBody(name='Subset',external_node_ids=ids[:1]));self.assertEqual(subscriptions.usage(subset)['used'],579,'upstream usage is account scoped even for subset')
        mixed=subscriptions.save(subscriptions.SubscriptionBody(name='Mixed',node_ids=[self.id],external_node_ids=ids));u=subscriptions.usage(mixed);self.assertEqual(u['scope'],'shared-node-port-aggregate');self.assertEqual(u['upstream_sources'][0]['download'],456)
    def test_older_editor_preserves_transform_options_and_invalid_usage_clears_previous(self):
        s=self.source(clean_prefixes=True,user_agent='clash.meta',address_overrides={'2606:4700::1111':'1.1.1.1'})
        updated=sources.save(sources.SourceBody(name='Edited'),s['id']);self.assertTrue(updated['clean_prefixes']);self.assertEqual(updated['user_agent'],'clash.meta');self.assertEqual(updated['address_overrides'],{'2606:4700::1111':'1.1.1.1'})
        self.assertEqual(sources.parse_usage('upload=12; download=34; total=100; expire=1900000000'),{'upload':12,'download':34,'total':100,'expire':1900000000})
        for header in ['', 'upload=-1;download=2;total=100','upload=2;total=100','upload=1;download=2;total=100;expire=9999999999999999']:
            self.assertEqual(sources.parse_usage(header),{})
