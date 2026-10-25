"""Low-frequency panel diagnostics; public liveness never exposes private state."""
import json
import shutil
import threading
import time
from fastapi import Request
from . import db

STARTED=time.time()
LAST_TICK=0
COMPONENTS={}
LOCK=threading.Lock()
LAST_CHECK=0


def record(name,ok):
    with LOCK:
        previous=COMPONENTS.get(name,{})
        COMPONENTS[name]={'state':'ok' if ok else 'failed','at':time.time(),
            'last_ok':time.time() if ok else previous.get('last_ok',0)}


def tick():
    global LAST_TICK,LAST_CHECK
    LAST_TICK=time.time()
    if LAST_TICK-LAST_CHECK<60:return
    LAST_CHECK=LAST_TICK
    from . import monitor,telemetry,notify_events
    c=db.get_conn()
    try:
        c.execute('SAVEPOINT health_write')
        c.execute("INSERT INTO settings(key,value) VALUES ('health_write_probe','ok') ON CONFLICT(key) DO UPDATE SET value='ok'")
        c.execute('ROLLBACK TO health_write');c.execute('RELEASE health_write')
        record('数据库读写',True)
    except Exception:
        c.rollback();record('数据库读写',False)
    record('磁盘可用空间',shutil.disk_usage(db.DATA_DIR).free>256*1024*1024)
    # Independent Monitor collection was removed; node scripts own heartbeats.
    cfg=telemetry.config()
    if cfg.get('enabled'):record('Telegram 最近投递',not db.get_setting('telegram_last_error'))
    queued=c.execute("SELECT count(*) FROM agent_jobs WHERE status IN ('queued','running') AND created_at<strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')").fetchone()[0]
    record('节点任务积压',queued==0)
    # One transition notification, using the existing bounded delivery queue.
    with LOCK:items=list(COMPONENTS.items())
    for name,item in items:
        key='panel_health:'+name;old=notify_events.get(key)
        notify_events.put(key,item['state'])
        if cfg.get('enabled') and cfg.get('alerts_health',True) and old!=item['state'] and (old is not None or item['state']=='failed'):
            telemetry.enqueue(key+':'+str(int(LAST_TICK)),telemetry.message_text('🩺 面板服务异常' if item['state']=='failed' else '✅ 面板服务恢复',
              [('组件',name),('结果','检查未通过' if item['state']=='failed' else '检查通过')],'请查看面板健康页；面板完全停机时无法自行通知。'),category='health',route='operations-module',priority=2)
    c.commit()


def snapshot():
    with LOCK:items={k:dict(v) for k,v in COMPONENTS.items()}
    now=time.time()
    for v in items.values():
        if now-v['at']>180:v['state']='unknown'
    items['后台调度']={'state':'ok' if LAST_TICK and now-LAST_TICK<180 else 'unknown' if now-STARTED<180 else 'failed','at':LAST_TICK}
    c=db.get_conn()
    jobs={r['status']:r['n'] for r in c.execute("SELECT status,count(*) n FROM agent_jobs WHERE status IN ('queued','running') GROUP BY status")}
    q=c.execute('SELECT count(*) n,coalesce(max(attempts),0) retries FROM notification_queue').fetchone()
    from . import schema,runtime_budget
    return {'generated_at':db.utcnow(),'components':items,'jobs':jobs,'notifications':dict(q),'database':schema.status(),'client_budget':runtime_budget.status(),
        'uptime_seconds':int(now-STARTED),'cloudflare':json.loads(db.get_setting('cloudflare_health','{}')),
        'note':'状态最多缓存 60 秒；未知不等于故障。Telegram 显示最近投递结果，不代表已主动验证凭据。面板完全停机需外部监控。'}


def register(app,owner):
    @app.get('/api/operations/health')
    def health(request:Request):
        owner(request);return snapshot()

    @app.post('/api/operations/health/cloudflare')
    def cf_check(request:Request):
        owner(request)
        from . import cloudflare
        try:
            cfg=cloudflare.settings()
            cloudflare.api('GET','/zones/'+cfg['zone_id'],config=cfg)
            result={'state':'ok','at':time.time(),'message':'Zone 读取通过；不验证 Tunnel 写入权限'}
        except Exception:result={'state':'failed','at':time.time(),'message':'无法读取 Zone，请检查凭据、权限和面板出站网络'}
        db.set_setting('cloudflare_health',json.dumps(result));return result
