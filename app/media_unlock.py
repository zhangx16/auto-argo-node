"""Bounded, on-demand page/region evidence through a selected node, not playback proof."""
import http.client
import json
import re
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor,as_completed
import time
from typing import Literal
from urllib.parse import urlsplit,urljoin,urlencode
from fastapi import HTTPException,Request
from pydantic import BaseModel,Field,field_validator
from . import db,external_nodes
from .proxy_runtime import runtime
from .media_extra import EXTRA,classify_extra,region_hint,visible_text

PLATFORMS={'netflix':'Netflix','disney':'Disney+','youtube':'YouTube Premium','prime':'Prime Video'}
TARGETS={'netflix':['https://www.netflix.com/title/81280792','https://www.netflix.com/title/70143836','https://www.netflix.com/title/80057281'],
 'disney':['https://www.disneyplus.com/'], 'youtube':['https://www.youtube.com/premium'], 'prime':['https://www.primevideo.com/']}
HOSTS={'netflix':('netflix.com',),'disney':('disneyplus.com','disney.api.edge.bamgrid.com'),'youtube':('youtube.com',),'prime':('primevideo.com','amazon.com')}
PLATFORMS.update({k:v['name'] for k,v in EXTRA.items()})
TARGETS.update({k:v['targets'] for k,v in EXTRA.items()})
HOSTS.update({k:v['hosts'] for k,v in EXTRA.items()})

LOCK=threading.RLock()
ACTIVE=None
WORKER=None
MAX_BYTES=1024*1024

class Options(BaseModel):
    platforms:list[Literal['netflix','disney','youtube','prime','apple_tv','spotify','youtube_video','tiktok','chatgpt','gemini','claude','grok']]=Field(default_factory=lambda:['netflix','disney','youtube','prime'],min_length=1,max_length=12)
    @field_validator('platforms')
    @classmethod
    def unique(cls,values):return list(dict.fromkeys(values))

def init():
    c=db.get_conn()
    c.execute('''CREATE TABLE IF NOT EXISTS media_checks(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
      node_id INTEGER NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,identity TEXT NOT NULL,results_json TEXT NOT NULL)''')
    c.execute("UPDATE media_checks SET status='interrupted' WHERE status='running'")
    # Both parent tables exist before this module is initialized.
    for kind,parent in [('managed','nodes'),('external','external_nodes')]:
        c.execute(f"CREATE TRIGGER IF NOT EXISTS media_delete_{kind} AFTER DELETE ON {parent} BEGIN DELETE FROM media_checks WHERE kind='{kind}' AND node_id=old.id; END")
    c.commit()

def node_link(kind,node_id,require_available=True):
    if kind=='managed':
        n=db.get_node(node_id)
        if not n or (require_available and n['status'] in ('removed','uninstalling')):raise HTTPException(404,'节点不存在或正在卸载')
        link=n['share_link']
    else:
        n=external_nodes.get(node_id)
        if require_available and not external_nodes.available(n):raise HTTPException(409,'外部节点已停用或来源中已失效')
        link=n['uri']
    if not link:raise HTTPException(409,'节点尚无可用链接')
    return link

def identity(link):return external_nodes.fingerprint(external_nodes.parse_uri(link))

def public(job):
    result={k:v for k,v in job.items() if k not in ('link','cancel','identity','deadline')}
    result['results']=[dict(r) for r in job.get('results',[])]
    return result

def update(job,**values):
    with LOCK:job.update(values)

def check(job):
    if job['cancel'].is_set():raise InterruptedError()
    if time.monotonic()>=job['deadline']:raise TimeoutError()

def fetch_page(port,url,platform,job,method='GET',body=None,headers=None):
    """Explicit CONNECT to loopback proxy; never consult env proxies or bypass lists."""
    for redirect in range(4):
        check(job);u=urlsplit(url);host=(u.hostname or '').lower()
        if u.scheme!='https' or not any(host==h or host.endswith('.'+h) for h in HOSTS[platform]):
            return {'code':0,'text':'','url':'','note':'跳转至登录、同意或验证页面，无法判断','truncated':False}
        connection=http.client.HTTPSConnection('127.0.0.1',port,timeout=min(6,max(.1,job['deadline']-time.monotonic())),context=ssl.create_default_context())
        connection.set_tunnel(host,443)
        try:
            request_headers={'Host':host,'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36','Accept-Language':'en-US,en;q=0.9','Accept-Encoding':'identity'}
            request_headers.update(headers or {})
            connection.request(method,(u.path or '/')+('?' + u.query if u.query else ''),body=body,headers=request_headers)
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                if method!='GET':return {'code':response.status,'text':'','url':'','note':'接口发生重定向，无法判断','truncated':False}
                location=response.getheader('Location')
                if not location or redirect==3:return {'code':response.status,'text':'','url':'','note':'重定向过多，无法判断','truncated':False}
                url=urljoin(url,location);continue
            body=bytearray()
            while len(body)<MAX_BYTES:
                check(job)
                if connection.sock:connection.sock.settimeout(min(3,max(.1,job['deadline']-time.monotonic())))
                chunk=response.read1(min(65536,MAX_BYTES-len(body)))
                if not chunk:break
                body.extend(chunk)
            with LOCK:job['bytes_received']+=len(body)
            return {'code':response.status,'text':body.decode('utf-8','replace'),'url':url,'note':'','truncated':len(body)>=MAX_BYTES}
        finally:connection.close()
    raise ValueError('页面重定向无效')

def classify(platform,pages):
    """Conservative evidence: generic 200/403, anti-bot and missing fields are never an unlock."""
    base={'platform':platform,'name':PLATFORMS[platform],'status':'unknown','region':'','detail':'页面未提供足够证据，无法判断'}
    texts=[p['text'] for p in pages];text='\n'.join(texts);lower=text.lower()
    if platform in EXTRA:base['region']=region_hint(platform,pages)
    negative=(visible_text(text) if platform in EXTRA else text).lower()
    if any(p.get('note') for p in pages):return base|{'detail':next(p['note'] for p in pages if p.get('note'))}
    if any(p['code']==429 for p in pages):return base|{'detail':'服务限流，请稍后再试'}
    if any(s in lower for s in ('cf-chl-','verify you are human','unusual traffic','robot check','data-uia="captcha"')):
        return base|{'detail':'遇到验证码或风控，无法判断'}
    if any(s in negative for s in ('not available in your country','not available in your region','not available in your location','service is not available in your area','geo_restricted','geo-blocked')):
        return base|{'status':'restricted','detail':'服务页面明确提示地区限制'}
    region=re.search(r'"(?:countryCode|currentCountry|currentTerritory|INNERTUBE_CONTEXT_GL)"\s*:\s*"([A-Z]{2})"',text)
    if region and not base['region']:base['region']=region[1]
    if platform in EXTRA:
        result=classify_extra(platform,pages,base)
        if result is not None:return result
    if platform=='netflix':
        def title_page(p):
            return p['code']==200 and ('data-uia="title-info"' in p['text'] or bool(re.search(r'"@type"\s*:\s*"(?:TVSeries|Movie)"',p['text'])))
        if any(title_page(p) for p in pages[:2]):return base|{'status':'available','detail':'抽样版权作品页面可用；未验证登录或实际播放'}
        if len(pages)>2 and title_page(pages[2]) and all(p['code']==404 for p in pages[:2]):return base|{'status':'limited','detail':'原创作品页面可用，抽样版权作品不可用；可能仅原创，也可能是片库差异'}
    elif platform=='youtube':
        if pages[0]['code']==200 and 'youtube premium' in lower and any(s in lower for s in ('get youtube premium','"offerid"','"premiumavailability"','premiumpurchasebutton','managesubscriptionbutton')):
            return base|{'status':'available','detail':'Premium 订阅页面提供可用方案；未验证付费播放'}
    elif platform=='disney':
        supported=re.search(r'"inSupportedLocation"\s*:\s*(true|false)',text)
        if supported:
            return base|{'status':'available' if supported[1]=='true' else 'restricted','detail':'服务接口确认支持该地区；未验证会员播放' if supported[1]=='true' else '服务接口明确表示当前地区不受支持'}
    elif platform=='prime':
        if pages[0]['code']==200 and region and ('primevideo' in lower or 'prime video' in lower):
            return base|{'status':'available','detail':'Prime Video 页面返回服务地区；未验证会员播放'}
    if any(p['code'] in (401,403) for p in pages):return base|{'detail':'访问被拒绝，可能是风控或权限要求，不能确认地区限制'}
    return base

# Public browser-client identifier, not a user/account secret. Session tokens stay in memory only.
DISNEY_PUBLIC_CLIENT='ZGlzbmV5JmJyb3dzZXImMS4wLjA.Cu56AgSfBTDag5NiRA81oLHkDZfu5L3CKadnefEAY84'

def disney_pages(port,job):
    base='https://disney.api.edge.bamgrid.com'
    headers={'Authorization':'Bearer '+DISNEY_PUBLIC_CLIENT,'Content-Type':'application/json'}
    first=fetch_page(port,base+'/devices','disney',job,'POST',json.dumps({'deviceFamily':'browser','applicationRuntime':'chrome','deviceProfile':'windows','attributes':{}}).encode(),headers)
    try:assertion=json.loads(first['text']).get('assertion')
    except ValueError:return [first]
    if not isinstance(assertion,str) or not assertion:return [first]
    form=urlencode({'grant_type':'urn:ietf:params:oauth:grant-type:token-exchange','latitude':0,'longitude':0,'platform':'browser','subject_token':assertion,'subject_token_type':'urn:bamtech:params:oauth:token-type:device'}).encode()
    second=fetch_page(port,base+'/token','disney',job,'POST',form,headers|{'Content-Type':'application/x-www-form-urlencoded'})
    try:data=json.loads(second['text'])
    except ValueError:return [second]
    if data.get('error_description')=='forbidden-location':return [second|{'text':'not available in your country'}]
    token=data.get('refresh_token')
    if not isinstance(token,str) or not token:return [second]
    query={'query':'mutation refreshToken($input: RefreshTokenInput!) {refreshToken(refreshToken: $input) {activeSession {sessionId}}}','variables':{'input':{'refreshToken':token}}}
    last=fetch_page(port,base+'/graph/v1/device/graphql','disney',job,'POST',json.dumps(query).encode(),headers)
    # Retain only region/support evidence, never session tokens in persisted results.
    return [last]

def execute(job):
    global ACTIVE
    status='completed';error='';running=set()
    try:
        with runtime(job['link'],job['cancel']) as port:
            def inspect(platform):
                check(job)
                with LOCK:
                    running.add(platform);job['current']=','.join(p for p in job['platforms'] if p in running)
                try:
                    pages=disney_pages(port,job) if platform=='disney' else [fetch_page(port,url,platform,job) for url in TARGETS[platform]]
                    return classify(platform,pages)
                except InterruptedError:raise
                except Exception:
                    expired=time.monotonic()>=job['deadline']
                    return {'platform':platform,'name':PLATFORMS[platform],'status':'skipped' if expired else 'error','region':'','detail':'达到总时限，此项目未完成' if expired else '连接超时、证书或网络异常；不能判断是否解锁'}
                finally:
                    with LOCK:
                        running.discard(platform);job['current']=','.join(p for p in job['platforms'] if p in running)
            with ThreadPoolExecutor(max_workers=2,thread_name_prefix='media-service') as pool:
                futures={pool.submit(inspect,p):p for p in job['platforms']}
                try:
                    for future in as_completed(futures):
                        platform=futures[future]
                        try:result=future.result()
                        except TimeoutError:
                            result={'platform':platform,'name':PLATFORMS[platform],'status':'skipped','region':'','detail':'达到总时限，尚未检测此项目'}
                        with LOCK:job['results'].append(result)
                except BaseException:
                    for future in futures:future.cancel()
                    raise
            if any(r['status']=='skipped' for r in job['results']):status='partial'
    except InterruptedError:status='cancelled'
    except Exception:status='failed';error='检测未完成，请检查节点转发或检测组件'
    finally:
        try:
            try:stale=identity(node_link(job['kind'],job['node_id']))!=job['identity']
            except Exception:stale=True
            with LOCK:job['results'].sort(key=lambda r:job['platforms'].index(r['platform']))
            update(job,status=status,error=error,stale=stale,current='')
            c=db.get_conn();c.execute('UPDATE media_checks SET status=?,results_json=? WHERE id=?',(status,json.dumps(public(job)),job['id']))
            c.execute('DELETE FROM media_checks WHERE id NOT IN (SELECT id FROM media_checks ORDER BY id DESC LIMIT 200)');c.commit()
        finally:
            with LOCK:
                if ACTIVE is job:ACTIVE=None
            if getattr(db._local,'conn',None):db._local.conn.close();db._local.conn=None


def history(kind,node_id):
    current_identity=identity(node_link(kind,node_id,False))
    rows=db.get_conn().execute('SELECT * FROM media_checks WHERE kind=? AND node_id=? ORDER BY id DESC LIMIT 5',(kind,node_id)).fetchall()
    with LOCK:active=public(ACTIVE) if ACTIVE else None
    records=[]
    for row in rows:
        record=json.loads(row['results_json']);record.update(id=row['id'],status=row['status'],created_at=row['created_at'],stale=bool(record.get('stale')) or row['identity']!=current_identity);records.append(record)
    return {'active':active,'history':records,'platforms':PLATFORMS}

def start(kind,node_id,body):
    global ACTIVE,WORKER
    link=node_link(kind,node_id);fp=identity(link)
    with LOCK:
        if ACTIVE:raise HTTPException(409,'已有解锁检测正在运行，请等待完成或取消')
        created=db.utcnow();c=db.get_conn()
        cur=c.execute('INSERT INTO media_checks(kind,node_id,created_at,status,identity,results_json) VALUES (?,?,?,\'running\',?,\'{}\')',(kind,node_id,created,fp));c.commit()
        job=dict(id=cur.lastrowid,kind=kind,node_id=node_id,created_at=created,status='running',platforms=body.platforms,current='',results=[],bytes_received=0,error='',stale=False,identity=fp,link=link,cancel=threading.Event(),deadline=time.monotonic()+60)
        ACTIVE=job;WORKER=threading.Thread(target=execute,args=(job,),daemon=True,name='media-check')
        try:WORKER.start()
        except RuntimeError:
            ACTIVE=None
            c.execute("UPDATE media_checks SET status='failed' WHERE id=?",(job['id'],));c.commit()
            raise HTTPException(503,'检测任务暂时无法启动')
        return public(job)

def shutdown():
    with LOCK:
        if ACTIVE:ACTIVE['cancel'].set()
        worker=WORKER
    if worker and worker.is_alive():worker.join(timeout=8)

def register(app,owner):
    @app.get('/api/media/{kind}/{node_id}')
    def get_results(kind:Literal['managed','external'],node_id:int,request:Request):owner(request);return history(kind,node_id)
    @app.post('/api/media/{kind}/{node_id}')
    def run(kind:Literal['managed','external'],node_id:int,body:Options,request:Request):owner(request);return start(kind,node_id,body)
    @app.delete('/api/media/{kind}/{node_id}')
    def cancel(kind:Literal['managed','external'],node_id:int,request:Request):
        owner(request)
        with LOCK:
            if not ACTIVE or ACTIVE['kind']!=kind or ACTIVE['node_id']!=node_id:raise HTTPException(404,'该节点没有正在运行的检测')
            ACTIVE['cancel'].set()
        return {'cancelled':True}
