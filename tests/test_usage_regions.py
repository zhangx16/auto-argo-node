import json,time,unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from pydantic import ValidationError
from app import db,regions,telemetry,subscriptions,external_nodes
import test_telemetry as tt
import test_subscriptions as st

class UsageAndRegionTests(unittest.TestCase):
    setUp=tt.TelemetryTests.setUp
    tearDown=tt.TelemetryTests.tearDown
    record=tt.TelemetryTests.record
    def ready(self):
        subscriptions.init();db.update_node(self.id,share_link='vless://341cad6d-9bf5-4b09-9c96-a7879ebc8fe1@8.8.8.8:443?security=tls&type=ws&path=%2Fws',host_ip='8.8.8.8')
    def test_local_database_cache_manual_override_and_revert(self):
        self.ready()
        with patch('urllib.request.urlopen',side_effect=AssertionError('No network lookup allowed')):
            self.assertEqual(regions.lookup('8.8.8.8'),'US')
            self.assertEqual(regions.lookup('127.0.0.1'),'');self.assertEqual(regions.lookup('::1'),'')
            regions.refresh_pending()
        n=regions.enrich(db.get_node(self.id));self.assertEqual(n['country_code'],'US');self.assertTrue(n['display_name'].startswith('🇺🇸'))
        c=db.get_conn();c.execute("UPDATE node_regions SET manual_code='JP',checked=0 WHERE node_id=?",(self.id,));c.commit();regions.refresh_pending()
        self.assertEqual(regions.enrich(db.get_node(self.id))['country_code'],'JP')
        with patch.object(regions,'lookup') as lookup:regions.refresh_pending();lookup.assert_not_called()
        c.execute('UPDATE node_regions SET manual_code=NULL WHERE node_id=?',(self.id,));c.commit()
        self.assertEqual(regions.enrich(db.get_node(self.id))['country_code'],'US')
        self.assertEqual(regions.display_name('🇰🇷 Example','JP'),'🇯🇵 Example')
        db.delete_node(self.id);self.assertEqual(c.execute('SELECT count(*) FROM node_regions').fetchone()[0],0)
    def test_invalid_regions_and_manual_unknown(self):
        for code in ['<script>','XX','USA','A1']:
            with self.assertRaises(ValidationError):regions.RegionBody(country_code=code)
        self.assertEqual(regions.RegionBody(country_code=' hk ').country_code,'HK')
        self.assertIsNone(regions.RegionBody(country_code=None).country_code)
        self.assertEqual(regions.flag('ZZ'),'')
    def test_history_bounded_fill_missing_and_lifetime_independent_of_retention(self):
        self.ready();self.record('a',100,100);self.record('a',200,300)
        c=db.get_conn();old=(datetime.now(timezone.utc).date()-timedelta(days=450)).isoformat()
        c.execute('UPDATE traffic_daily SET day=?',(old,));c.commit();self.record('a',220,330)
        h=telemetry.summary(7,self.id)['history'];self.assertEqual(len(h['points']),7);self.assertEqual(sum(p['measured'] for p in h['points']),1)
        self.assertEqual((h['points'][-1]['rx'],h['points'][-1]['tx']),(20,30))
        self.assertEqual((telemetry.summary()['totals']['rx'],telemetry.summary()['totals']['tx']),(120,230))
        self.assertEqual(c.execute('SELECT count(*) FROM traffic_daily WHERE day=?',(old,)).fetchone()[0],0)
        telemetry.init();self.assertEqual(telemetry.summary()['totals']['rx'],120)
        with self.assertRaises(Exception):telemetry.history(401)
    def test_shared_subscription_usage_headers_partial_and_legacy_preservation(self):
        self.ready();self.record('a',10,10);self.record('a',110,210)
        external=external_nodes.add(external_nodes.ConfirmBody(nodes=[external_nodes.NodeInput(uri='trojan://fixture-only@8.8.4.4:443#fixture')]))['node_ids'][0]
        s=subscriptions.save(subscriptions.SubscriptionBody(name='usage',node_ids=[self.id],external_node_ids=[external],traffic_limit_bytes=1024**3))
        u=subscriptions.usage(s);self.assertEqual((u['upload'],u['download'],u['used']),(100,200,300));self.assertEqual(u['external_count'],1);self.assertFalse(u['complete'])
        h=subscriptions.usage_headers(s);self.assertEqual(h['Subscription-Userinfo'],'upload=100; download=200; total=1073741824');self.assertEqual(h['X-Traffic-Coverage'],'partial')
        other=subscriptions.save(subscriptions.SubscriptionBody(name='second',node_ids=[self.id]));self.assertEqual(subscriptions.usage(other)['used'],300)
        updated=subscriptions.save(subscriptions.SubscriptionBody(name='renamed',node_ids=[self.id],external_node_ids=[external]),s['id']);self.assertEqual(updated['traffic_limit_bytes'],1024**3)
        external_only=subscriptions.save(subscriptions.SubscriptionBody(name='external',external_node_ids=[external]));self.assertNotIn('Subscription-Userinfo',subscriptions.usage_headers(external_only))
    def test_chain_only_counts_unique_hops_and_export_flags(self):
        self.ready();self.record('a',0,0);self.record('a',100,200)
        e=external_nodes.add(external_nodes.ConfirmBody(nodes=[external_nodes.NodeInput(uri='trojan://another-fixture@8.8.4.4:443#fixture')]))['node_ids'][0]
        hops=[{'kind':'managed','id':self.id},{'kind':'external','id':e}]
        s=subscriptions.save(subscriptions.SubscriptionBody(name='chain',node_ids=[self.id],external_node_ids=[e],chains=[{'name':'one','hops':hops},{'name':'two','hops':hops}],chain_only=True))
        self.assertEqual(subscriptions.usage(s)['used'],300)
        db.get_conn().execute('INSERT INTO node_regions(node_id,manual_code) VALUES (?,?)',(self.id,'JP'));db.get_conn().commit()
        s=subscriptions.save(subscriptions.SubscriptionBody(name='normal',node_ids=[self.id]))
        for fmt in ['raw','clash','singbox']:
            self.assertIn('🇯🇵',__import__('urllib.parse',fromlist=['unquote']).unquote(subscriptions.render(s,fmt)[0]))

class UsageHttpTests(unittest.TestCase):
    setUpClass=classmethod(st.SubscriptionTests.setUpClass.__func__)
    tearDownClass=classmethod(st.SubscriptionTests.tearDownClass.__func__)
    call=st.SubscriptionTests.call
    fetch=st.SubscriptionTests.fetch
    create_agent=st.SubscriptionTests.create_agent
    enroll_agent=st.SubscriptionTests.enroll_agent
    agent_call=st.SubscriptionTests.agent_call
    initial_success=st.SubscriptionTests.initial_success
    def test_history_and_region_api_auth_validation(self):
        for p in ['/api/traffic?days=7','/api/regions']:
            self.assertEqual(self.call(p,authed=False)[0],401)
        self.assertEqual(self.call('/api/traffic?days=401')[0],422)
        self.assertEqual(self.call('/api/traffic?node_id=999999')[0],404)
        id,token,meta=self.initial_success()
        code,d=self.call(f'/api/regions/managed/{id}','PUT',{'country_code':'JP'});self.assertEqual(code,200,d);self.assertEqual(d['country_code'],'JP')
        self.assertEqual(self.call(f'/api/regions/managed/{id}','PUT',{'country_code':'XX'})[0],422)
        self.assertEqual(self.call(f'/api/regions/managed/{id}','PUT',{'country_code':None})[0],200)
        code,s=self.call('/api/subscriptions','POST',{'name':'HTTP usage','node_ids':[id]});self.assertEqual(code,200,s)
        self.assertIn('usage',s);self.assertEqual(s['usage']['unmeasured_count'],1)
        report={'state':'online','metadata':meta,'process_alive':True,'local_ws':True,'public_ws':True,'traffic':{'epoch':'fixture','rx':100,'tx':200}}
        self.assertEqual(self.agent_call('/agent/report',token,report)[0],200)
        report['traffic'].update(rx=150,tx=300);self.assertEqual(self.agent_call('/agent/report',token,report)[0],200)
        for fmt in s['urls']:
            code,body,headers=self.fetch(s['urls'][fmt]);self.assertEqual(code,200,body)
            info=next(v for k,v in headers.items() if k.lower()=='subscription-userinfo');self.assertIn('upload=50',info);self.assertIn('download=100',info)
