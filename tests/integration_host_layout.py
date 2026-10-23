"""Persistent host grouping/sorting API in a disposable panel database, without a hub."""
import sqlite3
import test_api

cls=test_api.ApiTests;cls.setUpClass();client=cls()
try:
    c=sqlite3.connect(cls.tmp.name+'/app.db')
    for i in range(3):c.execute('INSERT INTO monitor_hosts(id,name,hub_id,hub_token,token_hash,token_secret,sort_order) VALUES (?,?,?,?,?,?,?)',(f'qa-{i}',f'QA {i}',i+1,'unused',f'qa-hash-{i}','unused',i))
    c.commit()
    path='/api/monitor/hosts/qa-0/group';body={'group_name':'亚洲 / 家宽'}
    assert client.call(path,'PUT',body,authed=False)[0]==401
    assert client.call(path,'PUT',body,origin='https://other.example.com')[0]==403
    assert client.call(path,'PUT',{'group_name':'bad\nname'})[0]==422
    assert client.call(path,'PUT',body)[0]==200
    group_path='/api/monitor/hosts/missing/group'
    assert client.call(group_path,'PUT',body)[0]==404
    order='/api/monitor/hosts/order'
    assert client.call(order,'PUT',{'host_ids':['qa-2','qa-0']},authed=False)[0]==401
    assert client.call(order,'PUT',{'host_ids':['qa-2','qa-0']},origin='https://other.example.com')[0]==403
    assert client.call(order,'PUT',{'host_ids':['qa-0','qa-0']})[0]==422
    assert client.call(order,'PUT',{'host_ids':['qa-0','missing']})[0]==409
    assert client.call(order,'PUT',{'host_ids':['qa-2','qa-0']})[0]==200
    status,data=client.call('/api/monitor/hosts')
    assert status==200 and [h['id'] for h in data['hosts']]==['qa-2','qa-1','qa-0']
    assert data['hosts'][-1]['group_name']=='亚洲 / 家宽'
    assert c.execute("SELECT group_name FROM monitor_hosts WHERE id='qa-0'").fetchone()[0]=='亚洲 / 家宽'
    assert [r[0] for r in c.execute('SELECT id FROM monitor_hosts ORDER BY sort_order,name,id')]==['qa-2','qa-1','qa-0']
    print('PASS real API: persisted classification, filtered ordering, auth/origin protection, stale IDs and validation; works while hub unavailable')
finally:cls.tearDownClass()
