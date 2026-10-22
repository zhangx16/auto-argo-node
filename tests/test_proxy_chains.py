import base64,json,unittest
import yaml
import test_subscriptions as st
from app import proxy_chains as pc,node_formats as nf

class ChainApiTests(unittest.TestCase):
    setUpClass=classmethod(st.SubscriptionTests.setUpClass.__func__)
    tearDownClass=classmethod(st.SubscriptionTests.tearDownClass.__func__)
    call=st.SubscriptionTests.call
    fetch=st.SubscriptionTests.fetch

    def sample(self,count=3):
        links=['ss://'+nf.b64encode('aes-128-gcm:chain-fixture-'+self._testMethodName+'-'+str(i))+f'@hop{i}.example.com:{21000+i}#Hop{i}' for i in range(count)]
        code,result=self.call('/api/external-nodes/import','POST',{'nodes':[{'uri':u,'name':'Hop'+str(i)} for i,u in enumerate(links)]})
        self.assertEqual(code,200,result)
        ids=result['node_ids'];refs=[{'kind':'external','id':i} for i in ids]
        body={'name':'Chain fixture','external_node_ids':ids,'chains':[{'name':'Main chain','hops':refs}],'chain_only':True}
        code,sub=self.call('/api/subscriptions','POST',body);self.assertEqual(code,200,sub)
        return sub,body,ids,links

    def test_order_and_formats_and_no_bypass(self):
        sub,body,ids,_=self.sample()
        self.assertEqual(sub['formats'],['clash','singbox']);self.assertEqual(sub['chain_count'],1)
        self.assertEqual(sub['exportable_count'],1);self.assertNotIn('raw',sub['urls'])
        for fmt in ['raw','base64']:
            self.assertEqual(self.call(f"/api/subscriptions/{sub['id']}/preview?format={fmt}")[0],409)
        for fmt in ['clash','singbox']:
            code,output=self.call(f"/api/subscriptions/{sub['id']}/preview?format={fmt}");self.assertEqual(code,200,output)
            self.assertEqual(output['node_count'],1)
            data=yaml.safe_load(output['content']) if fmt=='clash' else json.loads(output['content'])
            proxies=data['proxies'] if fmt=='clash' else [p for p in data['outbounds'] if p['type']=='shadowsocks']
            self.assertEqual([p['server'] for p in proxies],['hop0.example.com','hop1.example.com','hop2.example.com'])
            key='name' if fmt=='clash' else 'tag';via='dialer-proxy' if fmt=='clash' else 'detour'
            self.assertNotIn(via,proxies[0])
            self.assertEqual(proxies[1][via],proxies[0][key]);self.assertEqual(proxies[2][via],proxies[1][key])
            choices=data['proxy-groups'][0]['proxies'] if fmt=='clash' else data['outbounds'][0]['outbounds']
            self.assertEqual(choices,[proxies[-1][key]])
            if fmt=='singbox':self.assertEqual(data['outbounds'][0]['default'],proxies[-1]['tag'])

    def test_multiple_chains_clone_without_changing_plain_nodes(self):
        sub,body,ids,_=self.sample(2)
        body['chain_only']=False
        body['chains'].append({'name':'Reverse','hops':list(reversed(body['chains'][0]['hops']))})
        code,saved=self.call(f"/api/subscriptions/{sub['id']}",'PUT',body);self.assertEqual(code,200,saved)
        code,out=self.call(f"/api/subscriptions/{sub['id']}/preview?format=clash");data=yaml.safe_load(out['content'])
        self.assertEqual(out['node_count'],4);self.assertEqual(len(data['proxies']),6)
        self.assertNotIn('dialer-proxy',data['proxies'][0]);self.assertNotIn('dialer-proxy',data['proxies'][1])
        names=[p['name'] for p in data['proxies']];self.assertEqual(len(set(names)),6)
        self.assertTrue(data['proxy-groups'][0]['proxies'][0].startswith('链式'))
        self.assertEqual(data['proxies'][3]['dialer-proxy'],data['proxies'][2]['name'])
        self.assertEqual(data['proxies'][5]['dialer-proxy'],data['proxies'][4]['name'])

    def test_bad_references_and_loops_rejected_atomically(self):
        sub,body,ids,_=self.sample(2)
        for hops,expected in [([{'kind':'external','id':ids[0]}]*2,422),
                              ([{'kind':'external','id':ids[0]},{'kind':'managed','id':ids[1]}],409),
                              ([{'kind':'chain','id':1},{'kind':'external','id':ids[1]}],422)]:
            candidate=dict(body,chains=[{'name':'Bad chain','hops':hops}])
            self.assertEqual(self.call(f"/api/subscriptions/{sub['id']}",'PUT',candidate)[0],expected)
        self.assertEqual(self.call('/api/subscriptions','POST',dict(body,chains=[],chain_only=True))[0],422)
        saved=next(s for s in self.call('/api/subscriptions')[1] if s['id']==sub['id'])
        self.assertEqual(saved['chains'],body['chains'])

    def test_removed_hop_fails_closed_and_plain_mode_can_restore(self):
        sub,body,ids,_=self.sample(2)
        self.call(f'/api/external-nodes/{ids[0]}','DELETE')
        saved=next(s for s in self.call('/api/subscriptions')[1] if s['id']==sub['id'])
        self.assertTrue(saved['chain_errors']);self.assertEqual(saved['exportable_count'],0)
        self.assertEqual(self.fetch(sub['urls']['singbox'])[0],409)
        body.update(external_node_ids=[ids[1]],chains=[],chain_only=False)
        code,saved=self.call(f"/api/subscriptions/{sub['id']}",'PUT',body);self.assertEqual(code,200,saved)
        self.assertIn('base64',saved['urls']);self.assertEqual(self.fetch(saved['urls']['base64'])[0],200)

    def test_latest_hop_config_and_disable(self):
        sub,body,ids,links=self.sample(2)
        code,_=self.call(f'/api/external-nodes/{ids[0]}','PUT',{'name':'Updated','uri':links[0].replace(':21000',':32100'),'enabled':True})
        self.assertEqual(code,200)
        code,out=self.call(f"/api/subscriptions/{sub['id']}/preview?format=singbox")
        p=json.loads(out['content'])['outbounds'][1];self.assertEqual(p['server_port'],32100)
        self.call(f'/api/external-nodes/{ids[0]}','PUT',{'name':'Updated','uri':links[0].replace(':21000',':32100'),'enabled':False})
        self.assertEqual(self.fetch(sub['urls']['clash'])[0],409)

    def test_omitted_chain_fields_preserve_chain_on_update(self):
        sub,body,ids,_=self.sample(2)
        body.pop('chains');body.pop('chain_only');body['enabled']=False
        code,saved=self.call(f"/api/subscriptions/{sub['id']}",'PUT',body)
        self.assertEqual(code,200,saved);self.assertEqual(saved['chains'],sub['chains'])
        self.assertTrue(saved['chain_only']);self.assertEqual(saved['formats'],['clash','singbox'])

    def test_namespace_and_reserved_names(self):
        parsed={('managed',1):nf.parse_uri('ss://'+nf.b64encode('aes-128-gcm:one')+'@one.example.com:443#one'),
                ('external',1):nf.parse_uri('ss://'+nf.b64encode('aes-128-gcm:two')+'@two.example.com:443#two')}
        chain={'name':'proxy','hops':[{'kind':'managed','id':1},{'kind':'external','id':1}]}
        expanded,names=pc.expand([chain],parsed,False)
        self.assertEqual([p['server'] for p,_ in expanded],['one.example.com','two.example.com'])
        self.assertNotIn(names[0],['proxy','DIRECT','direct','节点选择'])
