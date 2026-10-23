"""Bounded node traffic accounting and persisted Telegram alert delivery."""
import json
import html
from urllib.parse import urlsplit
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, Request, Query
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
    alerts_health: bool = True
    alerts_tasks: bool = True
    alerts_changes: bool = True
    alerts_sources: bool = True
    alerts_expiry: bool = True
    alerts_resources: bool = True
    alerts_quota: bool = True


def init():
    db.get_conn().executescript('''
    CREATE TABLE IF NOT EXISTS traffic_samples(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      epoch TEXT NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL, sampled_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS traffic_daily(node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
      day TEXT NOT NULL, rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(node_id,day));
    CREATE TABLE IF NOT EXISTS traffic_totals(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE, rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0, since TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS notification_states(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      state TEXT NOT NULL, pending_state TEXT NOT NULL DEFAULT '', pending_since INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS notification_queue(id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
      text TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL, created INTEGER NOT NULL);
    ''')
    c=db.get_conn()
    columns={r['name'] for r in c.execute('PRAGMA table_info(notification_queue)')}
    for name,definition in [('category',"TEXT NOT NULL DEFAULT 'system'"),('route',"TEXT NOT NULL DEFAULT 'node-module'"),('priority','INTEGER NOT NULL DEFAULT 1')]:
        if name not in columns:c.execute(f'ALTER TABLE notification_queue ADD COLUMN {name} {definition}')
    c.execute('CREATE TABLE IF NOT EXISTS notification_deliveries(event_key TEXT PRIMARY KEY, sent INTEGER NOT NULL)')
    from . import notify_events
    notify_events.init()
    db.get_conn().execute('INSERT OR IGNORE INTO traffic_totals SELECT node_id,sum(rx),sum(tx),min(day) FROM traffic_daily GROUP BY node_id')
    db.get_conn().commit()


def config():
    return json.loads(db.get_setting('telegram_config','{}'))


def status():
    c=config()
    return {k:c.get(k,v) for k,v in {'chat_id':'','enabled':False,'offline_seconds':180,'summary_hours':24,'summary_enabled':True,**{'alerts_'+k:True for k in ('health','tasks','changes','sources','expiry','resources','quota')}}.items()} | {
        'configured':bool(c.get('token')), 'bot_username':c.get('bot_username',''),
        'last_error':db.get_setting('telegram_last_error'), 'last_sent':db.get_setting('telegram_last_sent'),
        'command_error':db.get_setting('telegram_command_error',''),
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
    for k in type(body).model_fields:
        if k.startswith('alerts_') and k not in body.model_fields_set:c[k]=old.get(k,True)
    raw=body.token.get_secret_value().strip()
    if raw:
        if not re.fullmatch(r'[0-9]{6,16}:[A-Za-z0-9_-]{20,128}',raw):raise HTTPException(422,'Bot Token 格式无效')
        c['token']=cloudflare.encrypt(raw)
    else:c['token']=old.get('token','')
    bot=telegram('getMe',cfg=c)
    c['bot_username']=bot.get('username','')
    if c['enabled'] and not c['chat_id']:raise HTTPException(422,'请先绑定通知聊天')
    db.set_setting('telegram_config',json.dumps(c))
    if c.get('enabled') and not old.get('enabled'):
        from . import notify_events
        notify_events.put('events_cursor',db.get_conn().execute('SELECT coalesce(max(id),0) FROM node_events').fetchone()[0]);db.get_conn().commit()
    if c.get('chat_id')!=old.get('chat_id') or c.get('token')!=old.get('token'):
        db.get_conn().execute('DELETE FROM notification_queue');db.get_conn().commit()
    if raw or c.get('chat_id')!=old.get('chat_id'):
        from . import bot_commands
        try:bot_commands.sync_menu(c)
        except HTTPException:db.set_setting('telegram_command_error','快捷菜单设置失败，可保存配置后重试；通知配置已保留。')
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
    c.execute('INSERT INTO traffic_totals VALUES (?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx',(node_id,rx,tx,day))
    # Daily aggregates only: bounded to 400 days, no minute-level history.
    c.execute("DELETE FROM traffic_daily WHERE day<date('now','-400 days')")


def summary(days=7,node_id=None):
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
    from . import regions
    totals={r['node_id']:dict(r) for r in c.execute('SELECT * FROM traffic_totals')}
    for n in nodes:
        n['available']=bool(n['sampled_at'] and now-n['sampled_at']<180)
        total=totals.get(n['id'],{})
        n['rx']=total.get('rx',n['rx']);n['tx']=total.get('tx',n['tx']);n['since']=total.get('since','')
        from . import quotas
        n['quota']=quotas.usage(n['id'])
        n.update({k:v for k,v in regions.enrich(n).items() if k.startswith('country_') or k=='display_name'})
    keys=['rx','tx','today_rx','today_tx','month_rx','month_tx']
    return {'nodes':nodes,'totals':{k:sum(n[k] for n in nodes) for k in keys},'timezone':'UTC','history':history(days,node_id),
      'scope':'需要节点 v4 脚本、nftables 和 NET_ADMIN 权限。节点监听端口 IN + OUT（Argo 为本地隧道到 sing-box 的流量），不等于网卡或厂商账单；从启用采集后累计，保留 400 天日汇总。'}


def history(days=7,node_id=None):
    if not 1<=days<=400:raise HTTPException(422,'时间范围必须为 1–400 天')
    c=db.get_conn()
    if node_id is not None and not db.get_node(node_id):raise HTTPException(404,'节点不存在')
    end=datetime.now(timezone.utc).date();start=end-timedelta(days=days-1)
    rows=c.execute('SELECT day,sum(rx) rx,sum(tx) tx FROM traffic_daily WHERE day>=? AND day<=?'+(' AND node_id=?' if node_id else '')+' GROUP BY day ORDER BY day',
      (start.isoformat(),end.isoformat(),node_id) if node_id else (start.isoformat(),end.isoformat())).fetchall()
    by_day={r['day']:dict(r) for r in rows}
    return {'days':days,'node_id':node_id,'timezone':'UTC','points':[{'date':(start+timedelta(days=i)).isoformat(),
      'rx':by_day.get((start+timedelta(days=i)).isoformat(),{}).get('rx',0),
      'tx':by_day.get((start+timedelta(days=i)).isoformat(),{}).get('tx',0),
      'measured':(start+timedelta(days=i)).isoformat() in by_day} for i in range(days)]}


def enqueue(key,text,category='system',route='node-module',priority=1):
    c=db.get_conn();now=int(time.time())
    if c.execute('SELECT 1 FROM notification_deliveries WHERE event_key=?',(key,)).fetchone():return
    # UTF-16 cap accounts for emoji while staying below Telegram's message limit.
    text=text.encode('utf-16-le')[:6800].decode('utf-16-le',errors='ignore')
    c.execute('INSERT OR IGNORE INTO notification_queue(event_key,text,due,created,category,route,priority) VALUES (?,?,?,?,?,?,?)',(key,text,now,now,category,route,priority))
    c.execute('DELETE FROM notification_queue WHERE id NOT IN (SELECT id FROM notification_queue ORDER BY priority DESC,id DESC LIMIT 200)')
    c.commit()


def bytes_text(value):
    for unit in ('B','KiB','MiB','GiB','TiB'):
        if abs(value)<1024 or unit=='TiB':return f'{value:.2f} {unit}'
        value/=1024


def age_seconds(stamp):
    from . import agent
    return agent.age(stamp)


def duration(seconds):
    seconds=max(0,int(seconds));minutes=seconds//60
    return f'{minutes//60} 小时 {minutes%60} 分钟' if minutes>=60 else f'{minutes} 分钟' if minutes else '不足 1 分钟'


def clean(value):
    return ''.join(c for c in str(value) if ord(c)>=32 and c not in '\u202a\u202b\u202c\u202d\u202e')[:200]


def node_name(node_id):
    from . import regions
    n=db.get_node(node_id)
    return clean(regions.enrich(n)['display_name']) if n else f'节点 #{node_id}'


def china_time(value=None):
    try:
        stamp=datetime.fromisoformat((value or db.utcnow()).replace('Z','+00:00'))
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')+'（北京时间 UTC+8）'
    except (ValueError,TypeError):return str(value or '')


def display_value(value):
    value=str(value)
    return china_time(value) if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?',value) else value


def message_text(title,fields=(),hint=''):
    lines=[title,'']+[f'{label}：{clean(display_value(value))}' for label,value in fields]
    if hint:lines+=['','💡 '+hint]
    return '\n'.join(lines+['',f'🕒 {china_time()}'])


def payload(text,cfg,route='node-module'):
    # Also normalize timestamps in messages queued before this upgrade.
    text=re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})(?: · UTC)?',lambda m:china_time(m.group(0).removesuffix(' · UTC')),text)
    lines=text.splitlines();formatted=[]
    for i,line in enumerate(lines):
        if i==0:formatted.append('<b>'+html.escape(line)+'</b>')
        elif '：' in line and not line.startswith(('💡','🕒')):
            label,value=line.split('：',1);formatted.append('<b>'+html.escape(label)+'：</b>'+html.escape(value))
        else:formatted.append(html.escape(line))
    result={'chat_id':cfg['chat_id'],'text':'\n'.join(formatted),'parse_mode':'HTML','disable_web_page_preview':True}
    base=db.get_setting('panel_url','').rstrip('/')
    try:u=urlsplit(base)
    except ValueError:u=None
    if u and u.scheme=='https' and u.hostname and not u.username and not u.password and not u.query and not u.fragment:
        routes={'node-module':'查看节点','task-module':'查看任务','subscription-module':'管理订阅','resources-module':'资源看板','traffic-module':'流量与通知'}
        route=route if route in routes else 'node-module'
        result['reply_markup']={'inline_keyboard':[[{'text':'🖥 '+routes[route],'url':base+'/#'+route}]]}
    return result


def summary_text():
    s=summary();t=s['totals'];nodes=s['nodes'];available=sum(n['available'] for n in nodes)
    counts={'正常':0,'异常':0,'待确认':0}
    for n in db.get_conn().execute("SELECT a.health_mode,a.health_status,a.last_report_at,a.probe_status,a.probe_at FROM node_agents a JOIN nodes n ON n.id=a.node_id WHERE a.bound=1 AND n.status NOT IN ('removed','uninstalling','pending')"):
        fresh_probe=age_seconds(n['probe_at'])<600
        host_unknown=n['health_mode']=='monitor' and db.get_setting('monitor_collector_state','online')!='online'
        if fresh_probe and n['probe_status']=='failed':counts['异常']+=1
        elif n['health_mode']!='none' and not host_unknown and (age_seconds(n['last_report_at'])>=config().get('offline_seconds',180) or n['health_status'] not in ('online','waiting','installing')):counts['异常']+=1
        elif fresh_probe and n['probe_status']=='ok' and not host_unknown and (n['health_mode']=='none' or n['health_status']=='online'):counts['正常']+=1
        else:counts['待确认']+=1
    lines=['📊 节点运行与流量简报','',f"节点状态：🟢 {counts['正常']} 正常 · 🔴 {counts['异常']} 异常 · ⚪ {counts['待确认']} 待确认",f'采集覆盖：{available} / {len(nodes)} 个节点',
      f"今日用量（UTC）：{bytes_text(t['today_rx']+t['today_tx'])}",f"  ↑ 上行 {bytes_text(t['today_rx'])}  ·  ↓ 下行 {bytes_text(t['today_tx'])}",
      f"本月用量（UTC）：{bytes_text(t['month_rx']+t['month_tx'])}",'','🏆 今日用量 TOP 5（UTC）']
    top=sorted(nodes,key=lambda n:n['today_rx']+n['today_tx'],reverse=True)[:5]
    peak=max([n['today_rx']+n['today_tx'] for n in top]+[1])
    for i,n in enumerate(top,1):
        used=n['today_rx']+n['today_tx'];bars=round(used/peak*8)
        lines+=[f"{i}. {clean(n.get('display_name',n['remark']))}",f"   {'▰'*bars}{'▱'*(8-bars)}  {bytes_text(used)}"+(' · 采集过期' if not n['available'] else '')]
    if not top:lines.append('暂无节点流量记录')
    if available<len(nodes):lines+=['',f'⚠️ {len(nodes)-available} 个节点尚未采集或数据过期，汇总可能不完整。']
    return '\n'.join(lines+['','端口统计，自启用后累计；非厂商账单。',f'🕒 {china_time()}'])


def tick():
    from . import agent
    c=config()
    from . import notify_events
    notify_events.tick(c)
    from . import bot_commands
    try:bot_commands.poll(c)
    except HTTPException:db.set_setting('telegram_command_error','快捷指令暂时无法读取，请检查 Bot 网络或其他程序是否同时使用此 Bot。')
    if not c.get('enabled') or not c.get('chat_id'):
        if c.get('token') and c.get('chat_id'):deliver(c,command_only=True)
        return
    conn=db.get_conn();now=int(time.time())
    rows=conn.execute("SELECT n.id,n.remark,n.status,a.last_report_at,a.health_status,a.probe_status,a.health_mode,a.process_alive,a.local_ws,a.management_enabled FROM nodes n JOIN node_agents a ON a.node_id=n.id WHERE a.bound=1 AND n.status NOT IN ('uninstalling','removed','pending')").fetchall()
    from . import monitor
    if any(n['health_mode']=='monitor' for n in rows):
        collector_ok=monitor.collector_available()
        collector_state='online' if collector_ok else 'offline'
        old_collector=db.get_setting('monitor_collector_state','online')
        pending=notify_events.get('collector_pending',{})
        if collector_state!=old_collector:
            if pending.get('state')!=collector_state:notify_events.put('collector_pending',{'state':collector_state,'since':now})
            elif now-pending['since']>=60:
                conn.execute("DELETE FROM notification_queue WHERE event_key LIKE 'monitor:%'")
                if notify_events.enabled(c,'health'):
                    enqueue('monitor:'+collector_state+':'+str(now),message_text('✅ Monitor 采集服务已恢复' if collector_ok else '⚠️ Monitor 采集服务不可用',hint='主机心跳提醒恢复。' if collector_ok else '暂缓探针心跳告警；实际协议转发检测继续运行。'),category='health',route='resources-module',priority=2)
                db.set_setting('monitor_collector_state',collector_state);notify_events.put('collector_pending',{})
        else:notify_events.put('collector_pending',{})
    else:collector_ok=True
    for n in rows:
        if n['health_mode']=='monitor' and not collector_ok and n['probe_status']!='failed':continue
        if n['health_mode']=='none' or (n['health_mode']=='monitor' and not collector_ok):
            if n['probe_status'] not in ('ok','failed'):continue
            state='online' if n['probe_status']=='ok' else 'degraded'
        else:state='offline' if agent.age(n['last_report_at'])>=c.get('offline_seconds',180) else n['health_status']
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
        outage_key=f'outage:{n["id"]}';outage=notify_events.get(outage_key)
        fields=[('节点',node_name(n['id']))]
        if state=='online':
            fields += [('恢复耗时',duration(now-outage))] if outage else []
            fields += [('转发检测','通过' if n['probe_status']=='ok' else '尚待面板验证')]
            notify_events.put(outage_key,None)
        else:
            if not outage:notify_events.put(outage_key,old['pending_since'] or now)
            if n['health_mode']!='none':fields.append(('最后心跳',n['last_report_at'] or '尚未收到'))
            reason='实际协议转发检查失败' if n['probe_status']=='failed' else '未收到新心跳；不等同于协议已失效' if state=='offline' else '代理进程未通过检查' if not n['process_alive'] else '本地监听未通过检查' if not n['local_ws'] else '节点状态持续异常'
            fields += [('检测结果',reason),('管理模式','已启用受控修复' if n['management_enabled'] else '手动管理')]
        # Keep only the latest pending health transition for a node.
        conn.execute("DELETE FROM notification_queue WHERE event_key LIKE ?",(f"node:{n['id']}:%",))
        if notify_events.enabled(c,'health'):
            enqueue(f"node:{n['id']}:{state}:{now}",message_text(('✅ ' if state=='online' else '🔴 ' if state=='offline' else '⚠️ ')+label,fields,'可打开面板查看当前状态与事件时间线。'),category='health',priority=2 if state!='online' else 1)
        conn.execute("UPDATE notification_states SET state=?,pending_state='',pending_since=0 WHERE node_id=?",(state,n['id']))
    conn.commit()
    last=int(db.get_setting('telegram_summary_at','0'))
    if c.get('summary_enabled',True) and now-last>=c.get('summary_hours',24)*3600:
        enqueue(f'summary:{now//3600}',summary_text(),category='summary',route='traffic-module',priority=0);db.set_setting('telegram_summary_at',str(now))
    deliver(c)


def deliver(c,command_only=False):
    from . import notify_events
    conn=db.get_conn();now=int(time.time())
    conn.execute('DELETE FROM notification_queue WHERE created<?',(now-86400,))
    conn.execute('DELETE FROM notification_deliveries WHERE sent<?',(now-30*86400,))
    conn.execute('DELETE FROM notification_deliveries WHERE event_key NOT IN (SELECT event_key FROM notification_deliveries ORDER BY sent DESC LIMIT 2000)')
    for category in notify_events.CATEGORIES:
        if not notify_events.enabled(c,category):conn.execute('DELETE FROM notification_queue WHERE category=?',(category,))
    if not c.get('summary_enabled',True):conn.execute("DELETE FROM notification_queue WHERE category='summary'")
    conn.commit()
    row=conn.execute('SELECT * FROM notification_queue WHERE due<=?'+(" AND category='command'" if command_only else '')+' ORDER BY priority DESC,id LIMIT 1',(now,)).fetchone()
    if not row:return
    try:
        telegram('sendMessage',payload(row['text'],c,row['route']),c)
        conn.execute('INSERT OR REPLACE INTO notification_deliveries VALUES (?,?)',(row['event_key'],now))
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
        owner(request)
        from . import bot_commands
        updates=telegram('getUpdates',{'limit':100,'timeout':0,'allowed_updates':['message','my_chat_member']});found={c['id']:c for c in bot_commands.recent_chats()}
        for u in updates:
            chat=(u.get('message') or u.get('my_chat_member') or {}).get('chat',{})
            if chat.get('id'):found[str(chat['id'])]={'id':str(chat['id']),'name':chat.get('title') or chat.get('first_name') or str(chat['id']),'type':chat.get('type','')}
        return list(found.values())
    @app.post('/api/notifications/test')
    def test(request:Request):
        owner(request);c=config()
        if not c.get('chat_id'):raise HTTPException(400,'请先绑定通知聊天')
        telegram('sendMessage',payload(message_text('🎉 通知通道连接成功',[('状态','这是一条测试消息，节点未发生故障'),('模式','异常去抖 · 恢复通知 · 有界重试')],'后续按你选择的通知分类推送。')+'\n\n'+summary_text(),c,'traffic-module'));return {'ok':True}
    @app.get('/api/traffic')
    def get_traffic(request:Request,days:int=Query(default=7,ge=1,le=400),node_id:int|None=Query(default=None,ge=1)):
        owner(request);return summary(days,node_id)
    @app.post('/api/traffic/notify')
    def notify(request:Request):
        owner(request);c=config()
        if not c.get('enabled') or not c.get('chat_id'):raise HTTPException(400,'请先启用通知并绑定聊天')
        enqueue('manual-summary:'+str(int(time.time())//60),summary_text(),category='manual',route='traffic-module',priority=0);return {'queued':True}
