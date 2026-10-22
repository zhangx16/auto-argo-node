"""Bounded node traffic accounting and persisted Telegram alert delivery."""
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from . import db, cloudflare

class Traffic(BaseModel):
    epoch: str = Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9:._-]+$')
    rx: int = Field(ge=0,le=2**63-1)
    tx: int = Field(ge=0,le=2**63-1)
    scope: str = Field(default='port',pattern=r'^port$')

class Config(BaseModel):
    token: SecretStr = Field(default=SecretStr(''),max_length=256)
    chat_id: str = Field(default='',pattern=r'^(?:-?[0-9]{1,24})?$')
    enabled: bool = False
    offline_seconds: int = Field(default=180,ge=180,le=3600)
    summary_hours: int = Field(default=24,ge=1,le=168)
    summary_enabled: bool = True


def init():
    db.get_conn().executescript('''
    CREATE TABLE IF NOT EXISTS traffic_samples(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      epoch TEXT NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL, sampled_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS traffic_daily(node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
      day TEXT NOT NULL, rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(node_id,day));
    CREATE TABLE IF NOT EXISTS notification_states(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      state TEXT NOT NULL, pending_state TEXT NOT NULL DEFAULT '', pending_since INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS notification_queue(id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
      text TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL, created INTEGER NOT NULL);
    ''')
    db.get_conn().commit()


def config():
    return json.loads(db.get_setting('telegram_config','{}'))


def status():
    c=config()
    return {k:c.get(k,v) for k,v in {'chat_id':'','enabled':False,'offline_seconds':180,'summary_hours':24,'summary_enabled':True}.items()} | {
        'configured':bool(c.get('token')), 'bot_username':c.get('bot_username',''),
        'last_error':db.get_setting('telegram_last_error'), 'last_sent':db.get_setting('telegram_last_sent'),
        'queued':db.get_conn().execute('SELECT count(*) FROM notification_queue').fetchone()[0]}


def telegram(method, payload=None, cfg=None):
    c=cfg or config()
    if not c.get('token'):raise HTTPException(400,'请先配置 Telegram Bot Token')
    try:
        token=cloudflare.decrypt(c['token'])
        req=urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,
            data=json.dumps(payload or {}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=12) as r:result=json.loads(r.read(262144))
        if not result.get('ok'):raise ValueError()
        return result['result']
    except Exception:
        # Never include the URL, token, or HTTP exception in errors/logs.
        raise HTTPException(502,'Telegram 请求失败，请检查 Bot 凭据、聊天权限及网络') from None


def configure(body):
    old=config();c=body.model_dump(exclude={'token'})
    raw=body.token.get_secret_value().strip()
    if raw:
        if not re.fullmatch(r'[0-9]{6,16}:[A-Za-z0-9_-]{20,128}',raw):raise HTTPException(422,'Bot Token 格式无效')
        c['token']=cloudflare.encrypt(raw)
    else:c['token']=old.get('token','')
    bot=telegram('getMe',cfg=c)
    c['bot_username']=bot.get('username','')
    if c['enabled'] and not c['chat_id']:raise HTTPException(422,'请先绑定通知聊天')
    db.set_setting('telegram_config',json.dumps(c))
    if c.get('chat_id')!=old.get('chat_id') or c.get('token')!=old.get('token'):
        db.get_conn().execute('DELETE FROM notification_queue');db.get_conn().commit()
    return status()


def record(node_id,sample):
    """Called within the accepted report's transaction; duplicate snapshots add zero."""
    if sample is None:return
    c=db.get_conn();now=int(time.time());old=c.execute('SELECT * FROM traffic_samples WHERE node_id=?',(node_id,)).fetchone()
    rx=tx=0
    # A new kernel counter generation starts at zero. Initial enrollment baselines
    # existing counters; later restarts retain the already accumulated panel totals.
    if old:
        if old['epoch']==sample.epoch:
            if sample.rx<old['rx'] or sample.tx<old['tx']:return
            rx=sample.rx-old['rx'];tx=sample.tx-old['tx']
        else:rx=sample.rx;tx=sample.tx
    c.execute('INSERT OR REPLACE INTO traffic_samples VALUES (?,?,?,?,?)',(node_id,sample.epoch,sample.rx,sample.tx,now))
    day=datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%d')
    c.execute('INSERT INTO traffic_daily VALUES (?,?,?,?) ON CONFLICT(node_id,day) DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx',(node_id,day,rx,tx))
    # Daily aggregates only: bounded to 400 days, no minute-level history.
    c.execute("DELETE FROM traffic_daily WHERE day<date('now','-400 days')")


def summary():
    c=db.get_conn();today=datetime.now(timezone.utc).strftime('%Y-%m-%d');month=today[:7]
    rows=c.execute('''SELECT n.id,n.remark,a.agent_version,s.sampled_at,
      coalesce(sum(d.rx),0) rx,coalesce(sum(d.tx),0) tx,
      coalesce(sum(CASE WHEN d.day=? THEN d.rx ELSE 0 END),0) today_rx,
      coalesce(sum(CASE WHEN d.day=? THEN d.tx ELSE 0 END),0) today_tx,
      coalesce(sum(CASE WHEN substr(d.day,1,7)=? THEN d.rx ELSE 0 END),0) month_rx,
      coalesce(sum(CASE WHEN substr(d.day,1,7)=? THEN d.tx ELSE 0 END),0) month_tx
      FROM nodes n LEFT JOIN node_agents a ON a.node_id=n.id
      LEFT JOIN traffic_samples s ON s.node_id=n.id LEFT JOIN traffic_daily d ON d.node_id=n.id
      GROUP BY n.id ORDER BY n.id''',(today,today,month,month)).fetchall()
    nodes=[dict(r) for r in rows];now=int(time.time())
    for n in nodes:n['available']=bool(n['sampled_at'] and now-n['sampled_at']<180)
    keys=['rx','tx','today_rx','today_tx','month_rx','month_tx']
    return {'nodes':nodes,'totals':{k:sum(n[k] for n in nodes) for k in keys},'timezone':'UTC',
      'scope':'需要节点 v4 脚本、nftables 和 NET_ADMIN 权限。节点监听端口 IN + OUT（Argo 为本地隧道到 sing-box 的流量），不等于网卡或厂商账单；从启用采集后累计，保留 400 天日汇总。'}


def enqueue(key,text):
    c=db.get_conn();now=int(time.time())
    c.execute('INSERT OR IGNORE INTO notification_queue(event_key,text,due,created) VALUES (?,?,?,?)',(key,text[:3500],now,now))
    c.execute('DELETE FROM notification_queue WHERE id NOT IN (SELECT id FROM notification_queue ORDER BY id DESC LIMIT 200)')
    c.commit()


def bytes_text(value):
    return f'{value/1024**3:.3f} GiB'


def summary_text():
    s=summary();t=s['totals']
    lines=['📊 节点流量汇总（UTC）',f"今日 IN {bytes_text(t['today_rx'])} / OUT {bytes_text(t['today_tx'])}",
      f"本月 IN {bytes_text(t['month_rx'])} / OUT {bytes_text(t['month_tx'])}"]
    for n in s['nodes'][:35]:lines.append(f"{n['remark'][:50]}：今日 {bytes_text(n['today_rx']+n['today_tx'])}"+('（采集未就绪/过期）' if not n['available'] else ''))
    return '\n'.join(lines)+'\n端口统计，自启用后累计；非厂商账单。'


def tick():
    from . import agent
    c=config()
    if not c.get('enabled') or not c.get('chat_id'):return
    conn=db.get_conn();now=int(time.time())
    rows=conn.execute("SELECT n.id,n.remark,n.status,a.last_report_at,a.health_status,a.probe_status FROM nodes n JOIN node_agents a ON a.node_id=n.id WHERE a.bound=1 AND n.status NOT IN ('uninstalling','removed','pending')").fetchall()
    for n in rows:
        state='offline' if agent.age(n['last_report_at'])>=c.get('offline_seconds',180) else n['health_status']
        if state in ['waiting','installing']:continue
        state='online' if state=='online' and n['probe_status']!='failed' else ('degraded' if state=='online' else state)
        old=conn.execute('SELECT * FROM notification_states WHERE node_id=?',(n['id'],)).fetchone()
        if old is None:
            conn.execute('INSERT INTO notification_states VALUES (?,?,?,?)',(n['id'],'online' if state!='online' else state,state if state!='online' else '',now));conn.commit();continue
        if state==old['state']:
            conn.execute("UPDATE notification_states SET pending_state='',pending_since=0 WHERE node_id=?",(n['id'],));continue
        if old['pending_state']!=state:
            conn.execute('UPDATE notification_states SET pending_state=?,pending_since=? WHERE node_id=?',(state,now,n['id']));continue
        if now-old['pending_since']<(0 if state=='offline' else 60):continue
        label={'offline':'心跳停止','online':'节点已恢复','degraded':'节点连接异常','network_error':'节点出站网络异常','repairing':'节点正在自动修复','error':'节点任务失败'}.get(state,state)
        enqueue(f"node:{n['id']}:{state}:{now}",f"{'✅' if state=='online' else '⚠️'} {label}\n节点：{n['remark'][:80]}\n最后心跳：{n['last_report_at']}\n时间：{db.utcnow()}")
        conn.execute("UPDATE notification_states SET state=?,pending_state='',pending_since=0 WHERE node_id=?",(state,n['id']))
    conn.commit()
    last=int(db.get_setting('telegram_summary_at','0'))
    if c.get('summary_enabled',True) and now-last>=c.get('summary_hours',24)*3600:
        enqueue(f'summary:{now//3600}',summary_text());db.set_setting('telegram_summary_at',str(now))
    # One request per panel tick; persisted bounded retries, no extra thread per node.
    row=conn.execute('SELECT * FROM notification_queue WHERE due<=? ORDER BY id LIMIT 1',(now,)).fetchone()
    if not row:return
    try:
        telegram('sendMessage',{'chat_id':c['chat_id'],'text':row['text'],'disable_web_page_preview':True},c)
        conn.execute('DELETE FROM notification_queue WHERE id=?',(row['id'],));conn.commit()
        db.set_setting('telegram_last_error','');db.set_setting('telegram_last_sent',db.utcnow())
    except HTTPException:
        attempts=row['attempts']+1
        if attempts>=8 or now-row['created']>86400:conn.execute('DELETE FROM notification_queue WHERE id=?',(row['id'],))
        else:conn.execute('UPDATE notification_queue SET attempts=?,due=? WHERE id=?',(attempts,now+min(3600,30*2**attempts),row['id']))
        conn.commit();db.set_setting('telegram_last_error','Telegram 发送失败，请检查聊天权限、凭据及网络；已安排有界重试。')


def register(app,require_login):
    def owner(request):
        require_login(request)
        from urllib.parse import urlsplit
        if request.headers.get('origin') and urlsplit(request.headers['origin']).netloc!=request.headers.get('host'):raise HTTPException(403,'请求来源不匹配')
    @app.get('/api/notifications')
    def get_config(request:Request):owner(request);return status()
    @app.put('/api/notifications')
    def put_config(body:Config,request:Request):owner(request);return configure(body)
    @app.post('/api/notifications/chats')
    def chats(request:Request):
        owner(request);updates=telegram('getUpdates',{'limit':100,'timeout':0,'allowed_updates':['message','my_chat_member']});found={}
        for u in updates:
            chat=(u.get('message') or u.get('my_chat_member') or {}).get('chat',{})
            if chat.get('id'):found[str(chat['id'])]={'id':str(chat['id']),'name':chat.get('title') or chat.get('first_name') or str(chat['id']),'type':chat.get('type','')}
        return list(found.values())
    @app.post('/api/notifications/test')
    def test(request:Request):
        owner(request);c=config()
        if not c.get('chat_id'):raise HTTPException(400,'请先绑定通知聊天')
        telegram('sendMessage',{'chat_id':c['chat_id'],'text':'✅ Argo 面板通知已连接。\n'+summary_text()});return {'ok':True}
    @app.get('/api/traffic')
    def get_traffic(request:Request):owner(request);return summary()
    @app.post('/api/traffic/notify')
    def notify(request:Request):
        owner(request);c=config()
        if not c.get('enabled') or not c.get('chat_id'):raise HTTPException(400,'请先启用通知并绑定聊天')
        enqueue('manual-summary:'+str(int(time.time())//60),summary_text());return {'queued':True}
