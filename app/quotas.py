"""Calendar-cycle quotas over existing daily counters; no remote changes or traffic blocking."""
import calendar
from datetime import date, datetime, timezone
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from . import db, events


class Rule(BaseModel):
    limit_bytes:int = Field(default=0,ge=0,le=2**60)
    reset_day:int = Field(default=1,ge=1,le=31)
    warning_percent:int = Field(default=80,ge=1,le=100)
    count_mode:Literal['both','rx','tx'] = 'both'


def init():
    db.get_conn().executescript('''
    CREATE TABLE IF NOT EXISTS traffic_quotas(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      limit_bytes INTEGER NOT NULL, reset_day INTEGER NOT NULL, warning_percent INTEGER NOT NULL, count_mode TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS traffic_quota_alerts(node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
      cycle_start TEXT NOT NULL, threshold INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(node_id,cycle_start,threshold));
    ''')
    db.get_conn().commit()


def cycle(reset_day,today=None):
    today = today or datetime.now(timezone.utc).date()
    def boundary(year,month):return date(year,month,min(reset_day,calendar.monthrange(year,month)[1]))
    start = boundary(today.year,today.month)
    if today < start:
        month = today.month-1 or 12
        start = boundary(today.year-(today.month==1),month)
    month = start.month%12+1
    end = boundary(start.year+(start.month==12),month)
    return start,end


def usage(node_id,today=None):
    c = db.get_conn()
    saved = c.execute('SELECT limit_bytes,reset_day,warning_percent,count_mode FROM traffic_quotas WHERE node_id=?',(node_id,)).fetchone()
    rule = dict(saved) if saved else Rule().model_dump()
    start,end = cycle(rule['reset_day'],today)
    row = c.execute('SELECT coalesce(sum(rx),0) rx,coalesce(sum(tx),0) tx,count(*) days FROM traffic_daily WHERE node_id=? AND day>=? AND day<?', (node_id,start.isoformat(),end.isoformat())).fetchone()
    used = row['rx']+row['tx'] if rule['count_mode']=='both' else row[rule['count_mode']]
    limit = rule['limit_bytes']
    return dict(rule, cycle_start=start.isoformat(), cycle_end=end.isoformat(), used_bytes=used,
                remaining_bytes=max(0,limit-used) if limit else None, percent=round(used/limit*100,2) if limit else None,
                measured_days=row['days'], exceeded=bool(limit and used>=limit), timezone='UTC')


def configure(node_id,body):
    if not db.get_node(node_id):raise HTTPException(404,'节点不存在')
    c = db.get_conn()
    old = c.execute('SELECT limit_bytes,reset_day,warning_percent,count_mode FROM traffic_quotas WHERE node_id=?',(node_id,)).fetchone()
    values = body.model_dump()
    if not old or dict(old)!=values:
        c.execute('INSERT INTO traffic_quotas VALUES (?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET limit_bytes=excluded.limit_bytes,reset_day=excluded.reset_day,warning_percent=excluded.warning_percent,count_mode=excluded.count_mode',
                  (node_id,body.limit_bytes,body.reset_day,body.warning_percent,body.count_mode))
        c.execute('DELETE FROM traffic_quota_alerts WHERE node_id=?',(node_id,))
        c.execute("DELETE FROM notification_queue WHERE event_key LIKE ?",(f'quota:{node_id}:%',))
        events.add(node_id,'quota','已更新流量额度与结算周期（只提醒，不停用节点）' if body.limit_bytes else '已关闭流量额度提醒')
        c.commit()
    return usage(node_id)


def tick():
    from . import telemetry
    c = db.get_conn();cfg=telemetry.config()
    for rule in c.execute('SELECT q.node_id,n.remark FROM traffic_quotas q JOIN nodes n ON n.id=q.node_id WHERE limit_bytes>0').fetchall():
        q = usage(rule['node_id'])
        for threshold in sorted(set([q['warning_percent'],100])):
            if q['used_bytes']*100 < q['limit_bytes']*threshold:continue
            old = c.execute('SELECT notified FROM traffic_quota_alerts WHERE node_id=? AND cycle_start=? AND threshold=?',(rule['node_id'],q['cycle_start'],threshold)).fetchone()
            label = '流量额度已用尽' if threshold==100 else f'流量用量达到 {threshold}% 提醒阈值'
            if not old:
                c.execute('INSERT INTO traffic_quota_alerts VALUES (?,?,?,0)',(rule['node_id'],q['cycle_start'],threshold))
                events.add(rule['node_id'],'quota',label+'；仅提醒，节点继续运行','warning')
                c.commit()
            if cfg.get('enabled') and cfg.get('chat_id') and cfg.get('alerts_quota',True) and (not old or not old['notified']):
                telemetry.enqueue(f"quota:{rule['node_id']}:{q['cycle_start']}:{threshold}",
                    telemetry.message_text('🚦 '+label,[('节点',telemetry.node_name(rule['node_id'])),('周期',q['cycle_start']+' → '+q['cycle_end']+'（不含，UTC）'),('用量',telemetry.bytes_text(q['used_bytes'])+' / '+telemetry.bytes_text(q['limit_bytes'])),('进度','▰'*min(10,int(q['percent']/10))+'▱'*max(0,10-int(q['percent']/10))+f" {q['percent']:.1f}%")],'端口统计，仅供参考；不会自动停用节点。'),category='quota',route='traffic-module',priority=2)
                c.execute('UPDATE traffic_quota_alerts SET notified=1 WHERE node_id=? AND cycle_start=? AND threshold=?',(rule['node_id'],q['cycle_start'],threshold))
                c.commit()
    c.execute("DELETE FROM traffic_quota_alerts WHERE cycle_start<date('now','-400 days')")
    c.commit()


def register(app,require_login):
    @app.get('/api/traffic/nodes/{node_id}/quota')
    def get_quota(node_id:int, request:Request):
        require_login(request)
        if not db.get_node(node_id):raise HTTPException(404,'节点不存在')
        return usage(node_id)
    @app.put('/api/traffic/nodes/{node_id}/quota')
    def set_quota(node_id:int, body:Rule, request:Request):
        require_login(request)
        from urllib.parse import urlsplit
        if request.headers.get('origin') and urlsplit(request.headers['origin']).netloc!=request.headers.get('host'):raise HTTPException(403,'请求来源不匹配')
        from . import agent
        with agent.LOCK:return configure(node_id,body)
