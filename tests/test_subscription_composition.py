"""Live references, account boundaries, safe chain composition and client usage."""
import json,unittest
from unittest.mock import patch
from fastapi import HTTPException
from app import subscriptions as subs,subscription_composition as comp,sources,external_nodes,db,proxy_chains
import test_reliability

class CompositionTests(unittest.TestCase):
    def setUp(self):test_reliability.ReliabilityTests.setUp(self);subs.init()
    tearDown=test_reliability.ReliabilityTests.tearDown
    def sub(self,name='Sub',**kwargs):return subs.save(subs.SubscriptionBody(name=name,**kwargs))
    def edit(self,row,**kwargs):return subs.save(subs.SubscriptionBody(name=row['name'],**kwargs),row['id'])
    def source(self,name,auth='first',total=10000):
        target=self.sub(name)
        with patch.object(sources.import_fetch,'public_target'):
            source=sources.save(sources.SourceBody(name=name,url='https://example.com/'+name,subscription_ids=[target['id']]))
        parsed=external_nodes.parse_content('trojan://'+auth+'@1.1.1.1:443?sni=tls.example.com#'+name)
        parsed['_usage']={'upload':100,'download':200,'total':total,'expire':1900000000}
        sources.apply(source['id'],source['revision'],parsed)
        return subs.get(target['id']),source
    def description(self,row):return subs.describe(row,lambda:'https://panel.example','test-secret')
    def test_live_references_deduplicate_diamond_and_authentication_stays_distinct(self):
        local=self.sub('Local',node_ids=[self.id]);a,_=self.source('A');b,_=self.source('B',auth='second')
        left=self.sub('Left',included_subscription_ids=[local['id'],a['id']])
        right=self.sub('Right',included_subscription_ids=[local['id'],a['id'],b['id']])
        combined=self.sub('Combined',node_ids=[self.id],included_subscription_ids=[left['id'],right['id']])
        text,_,_,count=subs.render(combined,'raw');self.assertEqual(count,3)
        self.assertEqual(text.count('trojan://'),2);self.assertIn('first@',text);self.assertIn('second@',text)
        self.assertEqual(self.description(combined)['node_ids'],[self.id],'inherited nodes must not be copied into direct selection')
        db.update_node(self.id,share_link=self.link.replace('old.trycloudflare.com','changed.trycloudflare.com'))
        self.assertIn('changed.trycloudflare.com',subs.render(combined,'raw')[0])
        self.edit(local,node_ids=[]);self.edit(combined,node_ids=[],included_subscription_ids=[left['id'],right['id']])
        self.assertEqual(subs.render(subs.get(combined['id']),'raw')[3],2)
    def test_identical_config_in_independent_source_records_deduplicates_in_export_and_grid(self):
        a,_=self.source('A');b,_=self.source('B')
        self.assertNotEqual(subs.external_members(a['id'])[0]['id'],subs.external_members(b['id'])[0]['id'])
        combined=self.sub(included_subscription_ids=[a['id'],b['id']])
        self.assertEqual(subs.render(combined,'raw')[3],1)
        self.assertEqual(len(self.description(combined)['resolved_external_node_ids']),1)
    def test_cycle_and_ancestor_depth_checks_roll_back_without_losing_membership(self):
        root=self.sub(node_ids=[self.id]);child=self.sub('Child',included_subscription_ids=[root['id']])
        for ids in [[root['id']],[child['id']]]:
            with self.assertRaises(HTTPException):self.edit(root,included_subscription_ids=ids)
            self.assertEqual(self.description(subs.get(root['id']))['node_ids'],[self.id])
        leaf=self.sub('Leaf');top=leaf
        for n in range(8):top=self.sub('Level '+str(n),included_subscription_ids=[top['id']])
        with self.assertRaises(HTTPException):self.edit(leaf,included_subscription_ids=[root['id']])
        self.assertEqual(comp.references(leaf['id']),[])
        with self.assertRaises(HTTPException):self.sub(included_subscription_ids=[9999])
        with self.assertRaises(HTTPException):self.sub(included_subscription_ids=[root['id']],usage_source_id=child['id'])
    def test_source_disabled_expired_deleted_and_restored_without_affecting_other_sources(self):
        a,_=self.source('A');b,_=self.source('B',auth='second')
        combined=self.sub(included_subscription_ids=[a['id'],b['id']],usage_source_id=a['id'])
        for changes,status in [({'enabled':False},'disabled'),({'expires_at':'2000-01-01T00:00:00Z'},'expired')]:
            self.edit(a,external_node_ids=[n['id'] for n in subs.external_members(a['id'])],**changes)
            desc=self.description(combined);self.assertEqual(desc['exportable_count'],1)
            self.assertEqual(next(x for x in desc['source_subscriptions'] if x['subscription_id']==a['id'])['status'],status)
            self.assertNotIn('Subscription-Userinfo',subs.usage_headers(combined),'never fall back to another account when explicitly selected source fails')
            self.edit(a,enabled=True,expires_at='',external_node_ids=[n['id'] for n in subs.external_members(a['id'])])
        db.get_conn().execute('DELETE FROM subscriptions WHERE id=?',(a['id'],));db.get_conn().commit()
        desc=self.description(combined);self.assertEqual(desc['exportable_count'],1);self.assertIn('missing',[r['status'] for r in desc['source_subscriptions']])
        self.edit(combined,included_subscription_ids=[a['id'],b['id']]) # old missing reference stays editable
        self.assertEqual(comp.references(combined['id'])[0]['source_name'],'A')
    def test_usage_once_per_account_and_selection_controls_client_headers(self):
        a,_=self.source('A');b,_=self.source('B',auth='second',total=30000)
        branch=self.sub('Branch',included_subscription_ids=[a['id']])
        combined=self.sub(included_subscription_ids=[a['id'],branch['id']])
        u=subs.usage(combined);self.assertEqual(u['meter_count'],1);self.assertEqual(len(u['breakdown']),1)
        self.assertEqual(u['client_usage']['used'],300);self.assertIn('total=10000',subs.usage_headers(combined)['Subscription-Userinfo'])
        combined=self.edit(combined,included_subscription_ids=[a['id'],b['id']])
        u=subs.usage(combined);self.assertEqual(u['meter_count'],2);self.assertIsNone(u['client_usage']);self.assertEqual(u['used'],0)
        self.assertNotIn('Subscription-Userinfo',subs.usage_headers(combined))
        combined=self.edit(combined,usage_source_id=b['id'])
        self.assertIn('total=30000',subs.usage_headers(combined)['Subscription-Userinfo'])
        self.assertEqual(subs.usage(combined)['client_usage_name'],'B')
        combined=self.edit(combined,enabled=False)
        self.assertEqual(combined['usage_source_id'],b['id']);self.assertEqual(len(comp.references(combined['id'])),2,'older clients retain new fields')
    def test_same_account_subsets_are_not_counted_twice_but_independent_accounts_are(self):
        a,_=self.source('A');ids=[n['id'] for n in subs.external_members(a['id'])]
        subset=self.sub('Subset',external_node_ids=ids)
        combined=self.sub(included_subscription_ids=[a['id'],subset['id']])
        u=subs.usage(combined);self.assertEqual(u['meter_count'],1);self.assertEqual(sum(bool(r['shared']) for r in u['breakdown']),1)
        self.assertEqual(subs.render(combined,'raw')[3],1)
    def test_inherited_chain_only_does_not_export_hops_or_direct_bypass(self):
        a,_=self.source('A');eid=subs.external_members(a['id'])[0]['id']
        chain={'name':'Route','hops':[{'kind':'managed','id':self.id},{'kind':'external','id':eid}]}
        original=self.sub('Chain',node_ids=[self.id],external_node_ids=[eid],chains=[chain],chain_only=True)
        combined=self.sub(included_subscription_ids=[original['id']])
        desc=self.description(combined);self.assertEqual(desc['formats'],['clash','singbox']);self.assertEqual(desc['exportable_count'],1)
        with self.assertRaises(HTTPException):subs.render(combined,'base64')
        conf=json.loads(subs.render(combined,'singbox')[0]);choices=conf['outbounds'][0]['outbounds']
        self.assertEqual(len(choices),1);self.assertNotIn('direct',choices)
        proxies=[p for p in conf['outbounds'] if p['type'] in ('vless','trojan')]
        self.assertEqual(len(proxies),2);self.assertEqual(proxies[1]['detour'],proxies[0]['tag'])
        # A standalone source may be added without exposing the chain's individual hops.
        b,_=self.source('B',auth='second');combined=self.edit(combined,included_subscription_ids=[original['id'],b['id']])
        conf=json.loads(subs.render(combined,'singbox')[0]);self.assertEqual(len(conf['outbounds'][0]['outbounds']),3) # chain + B + direct
        self.assertEqual(self.description(combined)['exportable_count'],2)
    def test_inherited_quarantine_removes_whole_chain_and_recovery_reappears(self):
        a,_=self.source('A');eid=subs.external_members(a['id'])[0]['id']
        chain={'name':'Route','hops':[{'kind':'managed','id':self.id},{'kind':'external','id':eid}]}
        original=self.sub('Chain',node_ids=[self.id],external_node_ids=[eid],chains=[chain],chain_only=True)
        combined=self.sub(included_subscription_ids=[original['id']])
        c=db.get_conn();c.execute('INSERT INTO subscription_health_policy VALUES (?,1,3,5,2,5)',(original['id'],))
        c.execute('INSERT INTO subscription_health_nodes(subscription_id,node_id,isolated) VALUES (?,?,1)',(original['id'],self.id));c.commit()
        self.assertEqual(self.description(combined)['exportable_count'],0)
        with self.assertRaises(HTTPException):subs.render(combined,'singbox')
        c.execute('UPDATE subscription_health_nodes SET isolated=0');c.commit()
        self.assertEqual(self.description(combined)['exportable_count'],1)
    def test_selected_local_source_expiry_is_preserved_in_client_header(self):
        original=self.sub('Local',node_ids=[self.id],expires_at='2029-01-01T00:00:00Z')
        combined=self.sub(included_subscription_ids=[original['id']],usage_source_id=original['id'])
        # No counters yet: expiry still comes from selected original subscription.
        self.assertIn('expire=1861920000',subs.usage_headers(combined)['Subscription-Userinfo'])

class CompositionApiTests(unittest.TestCase):
    st=test_reliability.st.SubscriptionTests
    setUpClass=classmethod(st.setUpClass.__func__)
    tearDownClass=classmethod(st.tearDownClass.__func__)
    call=st.call
    fetch=st.fetch
    def test_api_public_page_live_reference_and_cycle_rejection(self):
        code,result=self.call('/api/external-nodes/import','POST',{'nodes':[{'uri':'trojan://composition-fixture@1.1.1.1:443?sni=tls.example.com#API'}]})
        self.assertEqual(code,200,result)
        code,original=self.call('/api/subscriptions','POST',{'name':'API source','external_node_ids':result['node_ids']})
        self.assertEqual(code,200,original)
        code,parent=self.call('/api/subscriptions','POST',{'name':'API combined','included_subscription_ids':[original['id']],'usage_source_id':original['id']})
        self.assertEqual(code,200,parent);self.assertEqual(parent['exportable_count'],1)
        code,text,_=self.fetch(parent['urls']['raw']);self.assertEqual(code,200,text);self.assertIn('composition-fixture@',text)
        code,text,_=self.fetch(parent['page_url']+'/data');self.assertEqual(code,200,text)
        public=json.loads(text);self.assertEqual(len(public['nodes']),1);self.assertEqual(public['source_subscriptions'][0]['name'],'API source')
        self.assertNotIn(original['urls']['raw'],text);self.assertNotIn('token_version',text)
        code,error=self.call('/api/subscriptions/'+str(original['id']),'PUT',{'name':'API source','included_subscription_ids':[parent['id']]})
        self.assertEqual(code,422,error)
        self.assertEqual(self.fetch(parent['urls']['raw'])[0],200)
        code,_=self.call('/api/subscriptions/'+str(original['id']),'DELETE');self.assertEqual(code,200)
        code,text,_=self.fetch(parent['page_url']+'/data');self.assertEqual(code,200,text)
        public=json.loads(text);self.assertEqual(public['exportable_count'],0);self.assertEqual(public['source_subscriptions'][0]['status'],'missing')
        self.assertEqual(self.fetch(parent['urls']['raw'])[0],409)

    def test_bulk_delete_http_requires_owner_and_is_atomic(self):
        code,result=self.call('/api/external-nodes/import','POST',{'nodes':[{'uri':'trojan://batch-http-one@1.1.1.1:443#One'},{'uri':'trojan://batch-http-two@1.1.1.1:443#Two'}]})
        self.assertEqual(code,200,result);ids=result['node_ids']
        code,_=self.call('/api/external-nodes/bulk-delete','POST',{'node_ids':ids},authed=False);self.assertIn(code,(401,403))
        code,_=self.call('/api/external-nodes/bulk-delete','POST',{'node_ids':[ids[0],999999]});self.assertEqual(code,404)
        code,nodes=self.call('/api/external-nodes');self.assertTrue(set(ids)<={n['id'] for n in nodes})
        code,result=self.call('/api/external-nodes/bulk-delete','POST',{'node_ids':ids});self.assertEqual(code,200,result);self.assertEqual(result['deleted'],2)
        code,nodes=self.call('/api/external-nodes');self.assertFalse(set(ids)&{n['id'] for n in nodes})

class ExternalBatchTests(unittest.TestCase):
    setUp=CompositionTests.setUp
    tearDown=CompositionTests.tearDown
    sub=CompositionTests.sub
    source=CompositionTests.source
    description=CompositionTests.description
    def test_batch_delete_is_atomic_updates_every_subscription_and_stays_excluded(self):
        a,sa=self.source('Batch A');b,sb=self.source('Batch B',auth='second')
        ids=[subs.external_members(s['id'])[0]['id'] for s in (a,b)]
        parent=self.sub(included_subscription_ids=[a['id'],b['id']])
        with self.assertRaises(HTTPException):external_nodes.remove_many([ids[0],9999])
        self.assertEqual(subs.render(parent,'raw')[3],2)
        result=external_nodes.remove_many(ids);self.assertEqual(result['deleted'],2)
        self.assertEqual(self.description(parent)['exportable_count'],0)
        for source in (sa,sb):
            self.assertEqual(db.get_conn().execute('SELECT excluded FROM external_source_nodes WHERE source_id=?',(source['id'],)).fetchone()[0],1)
        parsed=external_nodes.parse_content('trojan://first@1.1.1.1:443?sni=tls.example.com#Batch%20A')
        sources.apply(sa['id'],sa['revision'],parsed)
        self.assertEqual(subs.external_members(a['id']),[])
    def test_batch_request_rejects_empty_duplicate_and_invalid_ids(self):
        from pydantic import ValidationError
        for ids in ([],[1,1],[-1],[0]):
            with self.assertRaises(ValidationError):external_nodes.DeleteBody(node_ids=ids)
