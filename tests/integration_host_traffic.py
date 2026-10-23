"""Host quota API against a disposable Monitor hub; MONITOR_HUB_URL must point to QA."""
import json,os,sqlite3,urllib.request
import test_api

hub=os.environ['MONITOR_HUB_URL'].rstrip('/')
assert '127.0.0.1:29881' in hub,'Only the dedicated QA hub is allowed'
def remote(path,method='GET',body=None):
    req=urllib.request.Request(hub+'/api'+path,method=method,headers={'X-Argo-Service':os.environ['ARGO_MONITOR_SERVICE_TOKEN'],'Content-Type':'application/json','Origin':'https://panel.example.com'},data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req,timeout=10) as r:return json.load(r)

cls=test_api.ApiTests;cls.setUpClass();client=cls();host_id=None
try:
    created=remote('/nodes','POST',{'name':'Disposable quota QA'})
    all_hosts=remote('/nodes');rows=all_hosts.get('nodes',[]) if isinstance(all_hosts,dict) else all_hosts
    host_id=next(h['id'] for h in rows if h['name']=='Disposable quota QA')
    c=sqlite3.connect(cls.tmp.name+'/app.db')
    c.execute("INSERT INTO monitor_hosts(id,name,hub_id,hub_token,token_hash,token_secret) VALUES('quota-qa','Quota QA',?,'unused','unused','unused')",(host_id,));c.commit()
    path='/api/monitor/hosts/quota-qa/traffic'
    body={'traffic_limit':1024**4,'traffic_mode':'up','traffic_reset_day':15}
    assert client.call(path,'PUT',body,authed=False)[0]==401
    assert client.call(path,'PUT',body,origin='https://other.example.com')[0]==403
    assert client.call(path,'PUT',body|{'traffic_limit':-1})[0]==422
    assert client.call(path,'PUT',body|{'name':'unexpected'})[0]==422
    assert client.call('/api/monitor/hosts/missing/traffic','PUT',body)[0]==404
    assert client.call(path,'PUT',body)[0]==200
    status,data=client.call('/api/monitor/hosts');q=data['hosts'][0]['traffic']
    assert status==200 and q['limit_bytes']==1024**4 and q['count_mode']=='up' and q['reset_day']==15,q
    assert q['percent']==0 and q['remaining_bytes']==1024**4,q
    assert client.call(path,'PUT',body|{'traffic_limit':0})[0]==200
    q=client.call('/api/monitor/hosts')[1]['hosts'][0]['traffic']
    assert q['limit_bytes']==0 and q['percent'] is None,q
    print('PASS real hub + panel: scoped host quota persistence, quota removal, auth, origin and validation guards')
finally:
    if host_id:remote('/nodes/'+str(host_id),'DELETE')
    cls.tearDownClass()
