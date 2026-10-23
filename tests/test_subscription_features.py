import json
import threading
import time
import unittest
from unittest.mock import patch,MagicMock
from fastapi import HTTPException
from pydantic import ValidationError
from app import db,agent,subscriptions,external_nodes,sources,labels,media_unlock
import test_reliability as rt
import test_subscriptions as st

URI='trojan://fixture-password@edge.example.com:443?sni=tls.example.com#Remote-A'

class FeatureTests(unittest.TestCase):
    def setUp(self):
        rt.ReliabilityTests.setUp(self);subscriptions.init()
    tearDown=rt.ReliabilityTests.tearDown
    def source(self,**kw):
        with patch.object(sources.import_fetch,'public_target'):
            return sources.save(sources.SourceBody(name='Source',url='https://example.com/private?token=fixture-secret',**kw))
    def parsed(self,uri=URI):return external_nodes.parse_content(uri)
    def apply(self,s,uri=URI):return sources.apply(s['id'],sources.get(s['id'])['revision'],self.parsed(uri))
    def test_media_worker_sanitizes_results_and_marks_changed_links_stale(self):
        from contextlib import contextmanager
        finished=threading.Event();entered=threading.Event();release=threading.Event()
        @contextmanager
        def fake_runtime(link,cancel):
            try:yield 12345
            finally:finished.set()
        def fake_fetch(*args,**kwargs):
            entered.set();release.wait(3)
            return dict(code=200,text='YouTube Premium premiumPurchaseButton "INNERTUBE_CONTEXT_GL":"US"',note='',truncated=False,url='https://www.youtube.com/premium')
        with patch.object(media_unlock,'runtime',fake_runtime),patch.object(media_unlock,'fetch_page',side_effect=fake_fetch):
            started=media_unlock.start('managed',self.id,media_unlock.Options(platforms=['youtube']))
            self.assertNotIn('link',started);self.assertTrue(entered.wait(2))
            with self.assertRaises(HTTPException):media_unlock.start('managed',self.id,media_unlock.Options())
            db.update_node(self.id,share_link=self.link.replace('old.example.com','changed.example.com'))
            release.set();self.assertTrue(finished.wait(3))
            for _ in range(100):
                if media_unlock.ACTIVE is None:break
                time.sleep(.02)
        result=media_unlock.history('managed',self.id)['history'][0]
        self.assertEqual(result['status'],'completed');self.assertTrue(result['stale']);self.assertEqual(result['results'][0]['region'],'US')
        self.assertNotIn(rt.UUID,json.dumps(result));self.assertNotIn('old.example.com',json.dumps(result))
        db.delete_node(self.id);self.assertEqual(db.get_conn().execute('SELECT count(*) FROM media_checks').fetchone()[0],0)
    def test_media_cancel_releases_runtime_and_stops_without_other_platforms(self):
        from contextlib import contextmanager
        entered=threading.Event();finished=threading.Event()
        @contextmanager
        def fake_runtime(link,cancel):
            try:yield 12345
            finally:finished.set()
        def fake_fetch(port,url,platform,job):
            entered.set();job['cancel'].wait(3);media_unlock.check(job)
        with patch.object(media_unlock,'runtime',fake_runtime),patch.object(media_unlock,'fetch_page',side_effect=fake_fetch):
            media_unlock.start('managed',self.id,media_unlock.Options())
            self.assertTrue(entered.wait(2));media_unlock.shutdown();self.assertTrue(finished.is_set());self.assertIsNone(media_unlock.ACTIVE)
        self.assertEqual(media_unlock.history('managed',self.id)['history'][0]['status'],'cancelled')
    def test_source_credentials_encrypted_and_never_described(self):
        s=self.source();stored=sources.get(s['id'])
        self.assertNotIn('fixture-secret',stored['url_cipher']);self.assertNotIn('url_cipher',json.dumps(s));self.assertNotIn('fixture-secret',json.dumps(s))
        with patch.object(sources.import_fetch,'public_target') as validate:
            updated=sources.save(sources.SourceBody(name='Renamed',url='',interval_minutes=15),s['id']);validate.assert_not_called()
        self.assertEqual(sources.get(s['id'])['url_cipher'],stored['url_cipher']);self.assertEqual(updated['revision'],2)
    def test_sync_preserves_ids_custom_name_country_labels_disabled_and_memberships(self):
        sub=subscriptions.save(subscriptions.SubscriptionBody(name='Target',node_ids=[self.id]))
        s=self.source(subscription_ids=[sub['id']]);self.apply(s)
        n=dict(db.get_conn().execute('SELECT * FROM external_nodes').fetchone());node_id=n['id']
        c=db.get_conn();c.execute('INSERT INTO external_labels VALUES (?,?,?)',(node_id,'家宽','["备用"]'));c.commit()
        body=external_nodes.EditBody(uri=n['uri'],name='My Custom Name',enabled=False)
        sources.edited(node_id,body)
        p=external_nodes.parse_uri(n['uri'],body.name)
        c.execute('UPDATE external_nodes SET name=?,uri=?,enabled=0 WHERE id=?',(body.name,external_nodes.to_uri(p),node_id));c.commit()
        self.apply(s,URI.replace('edge.example.com','new.example.com'))
        node=external_nodes.get(node_id)
        self.assertEqual(node['server'],'new.example.com');self.assertEqual(node['name'],'My Custom Name');self.assertEqual(node['enabled'],0)
        self.assertEqual(labels.enrich(node,'external')['group_name'],'家宽')
        self.assertEqual(subscriptions.external_members(sub['id'])[0]['id'],node_id)
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM external_nodes').fetchone()[0],1)
    def test_missing_return_and_parse_failure_preserve_selection(self):
        sub=subscriptions.save(subscriptions.SubscriptionBody(name='Target'))
        s=self.source(subscription_ids=[sub['id']]);self.apply(s)
        node_id=subscriptions.external_members(sub['id'])[0]['id']
        other=URI.replace('#Remote-A','#Remote-B').replace('edge.example.com','other.example.com')
        self.apply(s,other)
        self.assertFalse(external_nodes.available(external_nodes.get(node_id)))
        self.assertEqual(len(subscriptions.external_members(sub['id'])),2)
        self.apply(s,URI+'\n'+other)
        self.assertTrue(external_nodes.available(external_nodes.get(node_id)))
        before=[dict(r) for r in db.get_conn().execute('SELECT * FROM external_nodes')]
        with self.assertRaises(ValueError):sources.apply(s['id'],1,{'nodes':[],'invalid_count':1})
        self.assertEqual(before,[dict(r) for r in db.get_conn().execute('SELECT * FROM external_nodes')])
    def test_manual_deleted_source_node_not_resurrected_and_can_restore(self):
        s=self.source();self.apply(s);node_id=db.get_conn().execute('SELECT id FROM external_nodes').fetchone()[0]
        sources.removed(node_id);db.get_conn().execute('DELETE FROM external_nodes WHERE id=?',(node_id,));db.get_conn().commit()
        self.apply(s);self.assertEqual(db.get_conn().execute('SELECT count(*) FROM external_nodes').fetchone()[0],0)
        db.get_conn().execute('UPDATE external_source_nodes SET excluded=0');db.get_conn().commit();self.apply(s)
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM external_nodes').fetchone()[0],1)
    def test_stale_disabled_source_does_not_apply(self):
        s=self.source()
        with patch.object(sources.import_fetch,'public_target'):
            sources.save(sources.SourceBody(name='Source',enabled=False),s['id'])
        self.assertFalse(sources.apply(s['id'],1,self.parsed()))
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM external_nodes').fetchone()[0],0)
    def test_distinct_sources_do_not_overwrite_each_other_or_manual_node(self):
        manual=external_nodes.add(external_nodes.ConfirmBody(nodes=[external_nodes.NodeInput(uri=URI)]))['node_ids'][0]
        first=self.source();second=self.source();self.apply(first);self.apply(second)
        ids=[r[0] for r in db.get_conn().execute('SELECT node_id FROM external_source_nodes ORDER BY source_id')]
        self.assertNotEqual(ids[0],ids[1]);self.assertNotIn(manual,ids)
        self.apply(first,URI.replace('edge.example.com','changed.example.com'))
        self.assertEqual(external_nodes.get(ids[1])['server'],'edge.example.com');self.assertEqual(external_nodes.get(manual)['server'],'edge.example.com')
    def test_manual_config_override_preserved(self):
        s=self.source();self.apply(s);node=dict(db.get_conn().execute('SELECT * FROM external_nodes').fetchone())
        body=external_nodes.EditBody(name=node['name'],uri=node['uri'].replace('edge.example.com','manual.example.com'))
        sources.edited(node['id'],body)
        p=external_nodes.parse_uri(body.uri,body.name)
        db.get_conn().execute('UPDATE external_nodes SET uri=?,server=?,fingerprint=? WHERE id=?',(external_nodes.to_uri(p),p['server'],sources.storage_identity(s['id'],external_nodes.fingerprint(p)),node['id']));db.get_conn().commit()
        self.apply(s,URI.replace('edge.example.com','remote-changed.example.com'))
        self.assertEqual(external_nodes.get(node['id'])['server'],'manual.example.com')
    def test_source_scheduler_fetches_off_lock_and_cleans_up(self):
        s=self.source()
        with patch.object(sources.import_fetch,'fetch',return_value=URI) as fetch:
            self.assertTrue(sources.schedule(s['id']))
            for _ in range(100):
                if sources.RUNNING is None:break
                time.sleep(.02)
            self.assertIsNone(sources.RUNNING);fetch.assert_called_once()
            self.assertEqual(sources.describe(sources.get(s['id']))['node_count'],1)
        self.assertEqual(db.get_conn().execute('SELECT count(*) FROM agent_jobs').fetchone()[0],1)
    def test_label_validation_and_cascade(self):
        body=labels.LabelBody(group_name=' 家宽 ',tags=['备用','备用','流媒体'])
        self.assertEqual(body.group_name,'家宽');self.assertEqual(body.tags,['备用','流媒体'])
        for kw in [dict(tags=['x'*25]),dict(group_name='bad\nname'),dict(tags=['a']*13)]:
            with self.assertRaises(ValidationError):labels.LabelBody(**kw)
        db.get_conn().execute('INSERT INTO managed_labels VALUES (?,?,?)',(self.id,body.group_name,json.dumps(body.tags)));db.get_conn().commit()
        self.assertEqual(labels.enrich(db.get_node(self.id))['tags'],body.tags)
        db.delete_node(self.id);self.assertEqual(db.get_conn().execute('SELECT count(*) FROM managed_labels').fetchone()[0],0)

class MediaTests(unittest.TestCase):
    def page(self,text='',code=200,**kw):return dict(text=text,code=code,url='https://example.com',note='',truncated=False,**kw)
    def test_generic_200_403_and_captcha_not_unlock(self):
        for name in media_unlock.PLATFORMS:
            for page in [self.page('welcome'),self.page('Forbidden',403),self.page('verify you are human')]:
                result=media_unlock.classify(name,[page,page,page])
                self.assertEqual(result['status'],'unknown')
    def test_supported_restricted_and_partial_evidence(self):
        title=self.page('<div data-uia="title-info">title</div>')
        self.assertEqual(media_unlock.classify('netflix',[title,title,title])['status'],'available')
        self.assertEqual(media_unlock.classify('netflix',[self.page('',404),self.page('',404),title])['status'],'limited')
        youtube=self.page('YouTube Premium "INNERTUBE_CONTEXT_GL":"JP" premiumPurchaseButton')
        result=media_unlock.classify('youtube',[youtube]);self.assertEqual(result['region'],'JP');self.assertEqual(result['status'],'available')
        for name in media_unlock.PLATFORMS:self.assertEqual(media_unlock.classify(name,[self.page('not available in your country')])['status'],'restricted')
        self.assertEqual(media_unlock.classify('disney',[self.page('{"inSupportedLocation":false,"countryCode":"CN"}')])['status'],'restricted')
        self.assertEqual(media_unlock.classify('disney',[self.page('{"inSupportedLocation":true,"countryCode":"US"}')])['status'],'available')
        self.assertEqual(media_unlock.classify('prime',[self.page('Prime Video {"currentTerritory":"US"}')])['status'],'available')
    def test_additional_services_and_default_scope(self):
        self.assertEqual(len(media_unlock.PLATFORMS),12)
        self.assertEqual(len(media_unlock.Options().platforms),4)
        self.assertEqual(len(media_unlock.Options(platforms=list(media_unlock.PLATFORMS)).platforms),12)
        examples={
            'apple_tv':[self.page('US'),self.page('<title>Apple TV</title>')],
            'spotify':[self.page('Spotify signup')],
            'youtube_video':[self.page('ytInitialData "INNERTUBE_CONTEXT_GL":"JP"')],
            'tiktok':[self.page('__UNIVERSAL_DATA_FOR_REHYDRATION__ "region":"US"')],
            'chatgpt':[self.page('loc=US\n'),self.page('ChatGPT __reactRouterContext')],
            'gemini':[self.page('Gemini WIZ_global_data ,2,1,200,"USA"')],
            'claude':[self.page('loc=US\n'),self.page('Claude __next_data__')],
            'grok':[self.page('loc=US\n'),self.page('Grok Sign in')],
        }
        for platform,pages in examples.items():
            result=media_unlock.classify(platform,pages)
            self.assertIn(result['status'],['reachable','available'],(platform,result))
        self.assertEqual(media_unlock.classify('apple_tv',examples['apple_tv'])['status'],'reachable')
        self.assertEqual(media_unlock.classify('gemini',examples['gemini'])['region'],'USA')
        self.assertEqual(media_unlock.classify('chatgpt',examples['chatgpt'])['region'],'US')
        self.assertEqual(media_unlock.PLATFORMS['youtube'],'YouTube Premium');self.assertEqual(media_unlock.PLATFORMS['youtube_video'],'YouTube')
    def test_ai_trace_country_alone_does_not_prove_access_and_explicit_error_does(self):
        for platform in ['chatgpt','claude','grok']:
            result=media_unlock.classify(platform,[self.page('loc=US\n'),self.page('Forbidden',403)])
            self.assertEqual(result['status'],'unknown');self.assertEqual(result['region'],'US')
            error=self.page('{"error":{"code":"unsupported_country_region_territory"}}',403)
            self.assertEqual(media_unlock.classify(platform,[error])['status'],'restricted')
        script=self.page('ChatGPT __next<script>translation="not available in your country"</script>')
        self.assertEqual(media_unlock.classify('chatgpt',[script])['status'],'reachable')
        self.assertEqual(media_unlock.classify('spotify',[self.page('{"is_country_launched":false}')])['status'],'restricted')
    def test_explicit_loopback_connect_and_response_cap(self):
        connection=MagicMock();response=MagicMock();response.status=200;response.read1.side_effect=[b'x'*65536]*16
        connection.getresponse.return_value=response
        job={'cancel':threading.Event(),'deadline':time.monotonic()+20,'bytes_received':0}
        with patch.object(media_unlock.http.client,'HTTPSConnection',return_value=connection) as client:
            result=media_unlock.fetch_page(12345,'https://www.youtube.com/premium','youtube',job)
        self.assertEqual(client.call_args.args[:2],('127.0.0.1',12345));connection.set_tunnel.assert_called_with('www.youtube.com',443)
        self.assertTrue(result['truncated']);self.assertEqual(job['bytes_received'],1024*1024);connection.close.assert_called_once()
        with patch.object(media_unlock.http.client,'HTTPSConnection') as client:
            result=media_unlock.fetch_page(12345,'https://127.0.0.1/private','youtube',job);client.assert_not_called();self.assertTrue(result['note'])
    def test_disney_session_fields_used_without_persisting_tokens(self):
        pages=[self.page('{"assertion":"fixture-assertion"}'),self.page('{"refresh_token":"fixture-refresh"}'),self.page('{"extensions":{"sdk":{"session":{"countryCode":"US","inSupportedLocation":true}}}}')]
        with patch.object(media_unlock,'fetch_page',side_effect=pages):result=media_unlock.disney_pages(12345,{})
        self.assertEqual(len(result),1);self.assertNotIn('fixture-',result[0]['text'])

class FeatureHttpTests(unittest.TestCase):
    setUpClass=classmethod(st.SubscriptionTests.setUpClass.__func__)
    tearDownClass=classmethod(st.SubscriptionTests.tearDownClass.__func__)
    call=st.SubscriptionTests.call
    fetch=st.SubscriptionTests.fetch
    create_agent=st.SubscriptionTests.create_agent
    enroll_agent=st.SubscriptionTests.enroll_agent
    agent_call=st.SubscriptionTests.agent_call
    initial_success=st.SubscriptionTests.initial_success
    def test_portal_token_scope_rotation_expiry_and_minimal_metadata(self):
        node_id,_,_=self.initial_success()
        code,s=self.call('/api/subscriptions','POST',{'name':'Portal Test','node_ids':[node_id]});self.assertEqual(code,200,s)
        code,html,headers=self.fetch(s['page_url']);self.assertEqual(code,200);self.assertIn('portal-name',html);self.assertEqual({k.lower():v for k,v in headers.items()}['cache-control'],'no-store')
        code,raw,headers=self.fetch(s['page_url']+'/data');self.assertEqual(code,200);d=json.loads(raw)
        self.assertEqual(d['name'],'Portal Test');self.assertTrue(d['nodes']);self.assertNotIn('host_ip',raw);self.assertNotIn('token_version',raw)
        self.call(f"/api/subscriptions/{s['id']}/rotate",'POST')
        self.assertEqual(self.fetch(s['page_url'])[0],404);self.assertEqual(self.fetch(s['page_url']+'/data')[0],404)
        invalid=s['page_url'].rsplit('/',1)[0]+'/'+('%C3%A9'*64)
        self.assertEqual(self.fetch(invalid)[0],404)
        _,expired=self.call('/api/subscriptions','POST',{'name':'Expired','node_ids':[node_id],'expires_at':'2020-01-01T00:00:00Z'})
        self.assertEqual(self.fetch(expired['page_url'])[0],404)
        _,disabled=self.call('/api/subscriptions','POST',{'name':'Disabled','node_ids':[node_id],'enabled':False})
        self.assertEqual(self.fetch(disabled['page_url']+'/data')[0],404)
    def test_owner_source_labels_media_access_control(self):
        node_id,_,_=self.initial_success()
        for path in ['/api/external-sources','/api/labels',f'/api/media/managed/{node_id}']:
            self.assertEqual(self.call(path,authed=False)[0],401)
        self.assertEqual(self.call('/api/external-sources','POST',{'name':'Private','url':'http://127.0.0.1/private'})[0],422)
        self.assertEqual(self.call(f'/api/labels/managed/{node_id}','PUT',{'group_name':'家宽','tags':['备用']},origin='https://wrong.example')[0],403)
        code,body=self.call(f'/api/labels/managed/{node_id}','PUT',{'group_name':'家宽','tags':['备用']});self.assertEqual(code,200,body)
        _,nodes=self.call('/api/nodes');self.assertEqual(next(n for n in nodes if n['id']==node_id)['tags'],['备用'])
        self.assertEqual(self.call(f'/api/media/managed/{node_id}','POST',{'platforms':['invalid']})[0],422)
        self.assertEqual(self.call(f'/api/media/managed/{node_id}','DELETE')[0],404)
