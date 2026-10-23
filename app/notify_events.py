"""Panel-only notification rules over existing observations; no node workload."""
import json
import time
from datetime import datetime, timezone
from . import db

CATEGORIES = {
    'health':'异常与恢复', 'tasks':'部署与任务结果', 'changes':'修复与配置变更',
    'sources':'外部订阅异常', 'expiry':'订阅到期', 'resources':'持续资源压力', 'quota':'流量额度',
}


def init():
    c=db.get_conn()
    c.execute('''CREATE TABLE IF NOT EXISTS notification_observations(
      key TEXT PRIMARY KEY, value TEXT NOT NULL, updated INTEGER NOT NULL)''')
    c.commit()


def get(key, default=None):
    r=db.get_conn().execute('SELECT value FROM notification_observations WHERE key=?',(key,)).fetchone()
    return json.loads(r['value']) if r else default


def put(key, value):
    db.get_conn().execute('INSERT INTO notification_observations VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated',(key,json.dumps(value),int(time.time())))


def enabled(cfg, category):
    return cfg.get('alerts_'+category,True)


def event_notifications(cfg):
    from . import telemetry
    c=db.get_conn();latest=c.execute('SELECT coalesce(max(id),0) FROM node_events').fetchone()[0]
    cursor=get('events_cursor')
    if cursor is None or not cfg.get('enabled'):
        put('events_cursor',latest);return
    rows=c.execute('SELECT e.*,n.remark FROM node_events e JOIN nodes n ON n.id=e.node_id WHERE e.id>? ORDER BY e.id LIMIT 200',(cursor,)).fetchall()
    for e in rows:
        put('events_cursor',e['id'])
        if telemetry.age_seconds(e['created_at'])>600:continue
        kind=e['kind'];message=e['message'];category='changes';title='';hint='';urgent=e['level']=='warning'
        if kind=='job' and ('执行成功' in message or '执行失败' in message):
            category='tasks';title='❌ 节点任务失败' if urgent else '🛠 节点任务已执行'
            hint='请打开部署任务查看日志；执行完成不等于转发验证通过。'
        elif kind=='repair':
            title='🔧 节点已尝试自动修复';hint='面板将继续验证实际转发，请留意恢复通知。'
            key=f'repair_cooldown:{e["node_id"]}'
            if int(time.time())-get(key,0)<600:continue
            put(key,int(time.time()))
        elif kind=='tunnel':title='🔄 临时隧道已变更';hint='订阅已同步；请在客户端刷新订阅。实际转发仍需验证。'
        elif kind=='core':title='↩️ 核心回滚 / 验证结果';hint='请在面板核对核心版本和实际转发状态。'
        elif kind=='change' and message in ('新配置验证通过','已恢复修改前配置并通过验证','恢复后仍未通过验证，请人工检查','新配置验证失败，等待节点恢复原监听配置'):
            title='✅ 配置验证完成' if message in ('新配置验证通过','已恢复修改前配置并通过验证') else '↩️ 配置恢复提醒'
            hint='原有订阅会读取当前生效配置。'
        else:continue
        if enabled(cfg,category):telemetry.enqueue(f'event:{e["id"]}',telemetry.message_text(title,[('节点',telemetry.node_name(e['node_id'])),('结果',message)],hint),category=category,route='task-module' if kind=='job' else 'node-module',priority=2 if urgent else 1)


def subscription_notifications(cfg):
    from . import telemetry
    c=db.get_conn();now=int(time.time())
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='external_sources'").fetchone():
        from . import sources
        for r in c.execute('SELECT id,name,last_checked,last_success,last_error FROM external_sources WHERE enabled=1'):
            if sources.RUNNING==r['id']:continue
            key=f'source:{r["id"]}';old=get(key,{});stamp=r['last_checked']+'|'+r['last_success']+'|'+str(bool(r['last_error']))
            if old.get('stamp')==stamp:continue
            count=old.get('failures',0)+1 if r['last_error'] else 0
            notified=old.get('notified',False)
            if cfg.get('enabled') and enabled(cfg,'sources'):
                if count>=2 and not notified:
                    telemetry.enqueue(f'source:{r["id"]}:failed:{now}',telemetry.message_text('📥 外部订阅连续更新失败',[('来源',r['name']),('状态',f'连续 {count} 次更新失败')],'现有节点暂时保留。请检查来源有效期和面板出站网络。'),category='sources',route='subscription-module',priority=2);notified=True
                elif count==0 and notified:
                    telemetry.enqueue(f'source:{r["id"]}:recovered:{now}',telemetry.message_text('✅ 外部订阅更新已恢复',[('来源',r['name']),('状态','已成功获取并处理来源节点')]),category='sources',route='subscription-module');notified=False
            put(key,{'stamp':stamp,'failures':count,'notified':notified})
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='subscriptions'").fetchone():return
    for r in c.execute("SELECT id,name,expires_at FROM subscriptions WHERE enabled=1 AND expires_at!=''"):
        try:
            expiry=datetime.fromisoformat(r['expires_at'].replace('Z','+00:00'))
            if expiry.tzinfo is None:expiry=expiry.replace(tzinfo=timezone.utc)
        except ValueError:continue
        left=expiry.timestamp()-now;stage='expired' if left<=0 else 'soon' if left<=3*86400 else ''
        if not stage or left < -86400:continue
        key=f'expiry:{r["id"]}:{r["expires_at"]}:{stage}'
        if not cfg.get('enabled') or not enabled(cfg,'expiry'):
            put(key,True);continue
        if get(key):continue
        telemetry.enqueue(key,telemetry.message_text('⌛ 订阅已到期' if stage=='expired' else '🗓 订阅即将到期',[('订阅',r['name']),('到期时间',r['expires_at']),('状态','已到期' if stage=='expired' else f'剩余约 {max(1,int(left/3600))} 小时')],'到期后订阅导出不可用；不会停止节点上的协议服务。'),category='expiry',route='subscription-module',priority=2 if stage=='expired' else 1)
        put(key,True)


def resource_notifications(cfg):
    from . import monitor,telemetry
    now=int(time.time());c=db.get_conn()
    if not cfg.get('enabled') or not enabled(cfg,'resources'):return
    last=get('resource_poll',0)
    if now-last<60:return
    put('resource_poll',now)
    if not c.execute('SELECT 1 FROM monitor_hosts WHERE last_seen>? LIMIT 1',(now-90,)).fetchone():return
    try:remote=monitor.hub('/nodes')
    except Exception:
        # Missing samples break the sustained-pressure window.
        for r in c.execute("SELECT key,value FROM notification_observations WHERE key LIKE 'resource:%'").fetchall():
            state=json.loads(r['value']);state['since']=now;state['last']=0;state['high']=False;put(r['key'],state)
        return
    if isinstance(remote,dict):remote=remote.get('nodes',[])
    byid={h['id']:h for h in remote}
    for h in c.execute('SELECT id,hub_id,name,last_seen FROM monitor_hosts'):
        r=byid.get(h['hub_id'],{});m=r.get('metrics') or {}
        fresh=r.get('online') and now-h['last_seen']<90
        for metric,label in [('cpu','CPU'),('mem','内存'),('disk','磁盘')]:
            key=f'resource:{h["id"]}:{metric}';old=get(key,{'since':now,'last':0,'notified':False})
            value=m.get('cpu') if metric=='cpu' else 100*m.get(metric+'_used',0)/m[metric+'_total'] if m.get(metric+'_total',0)>0 else None
            if not fresh or value is None:
                old.update(since=now,last=0,high=False);put(key,old);continue
            if now-old.get('last',0)>120:old['since']=now
            if value>=90:
                if not old.get('high'):old['since']=now
                old['high']=True
                if now-old['since']>=300 and not old['notified']:
                    telemetry.enqueue(key+':high:'+str(now),telemetry.message_text('🔥 主机资源持续偏高',[('主机',h['name']),('指标',f'{label} {value:.1f}%'),('持续时间','至少 5 分钟 ≥ 90%')],'请检查高占用进程或磁盘空间；面板不会自动重启服务。'),category='resources',route='resources-module',priority=2);old['notified']=True
            else:
                old['since']=now;old['high']=False
                if value<80 and old['notified']:
                    telemetry.enqueue(key+':recovered:'+str(now),telemetry.message_text('🌿 主机资源压力已缓解',[('主机',h['name']),('指标',f'{label} {value:.1f}%')],'该指标已回落至 80% 以下。'),category='resources',route='resources-module');old['notified']=False
            old['last']=now;put(key,old)


def tick(cfg):
    event_notifications(cfg)
    subscription_notifications(cfg)
    resource_notifications(cfg)
    c=db.get_conn();c.execute('DELETE FROM notification_observations WHERE updated<?',(int(time.time())-400*86400,));c.commit()
