"""Bounded, credential-free node event timeline, independent of notification settings."""
import json
from fastapi import HTTPException, Request, Query
from . import db


def init():
    db.get_conn().executescript('''
    CREATE TABLE IF NOT EXISTS node_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
      node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL, kind TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_node_events ON node_events(node_id,id);
    CREATE TABLE IF NOT EXISTS node_event_states(node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
      kind TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(node_id,kind));
    ''')
    db.get_conn().commit()


def add(node_id, kind, message, level='info'):
    c = db.get_conn()
    c.execute('INSERT INTO node_events(node_id,created_at,kind,level,message) VALUES (?,?,?,?,?)',
              (node_id, db.utcnow(), kind, level, message[:500]))
    c.execute('DELETE FROM node_events WHERE id NOT IN (SELECT id FROM node_events ORDER BY id DESC LIMIT 5000)')


def observe(node_id, kind, value, message, level='info'):
    c = db.get_conn()
    old = c.execute('SELECT value FROM node_event_states WHERE node_id=? AND kind=?', (node_id,kind)).fetchone()
    if old and old['value'] == value:
        return
    c.execute('INSERT INTO node_event_states VALUES (?,?,?) ON CONFLICT(node_id,kind) DO UPDATE SET value=excluded.value', (node_id,kind,value))
    add(node_id,kind,message,level)


def health(node_id, state, process_alive, local_ws, public_ws, direct=False):
    reasons = []
    if state == 'offline': reasons.append('超过 180 秒未收到心跳；请检查主机、网络及监控脚本')
    elif state == 'network_error': reasons.append('节点出站网络检查失败')
    elif state not in ('installing','waiting'):
        if not process_alive: reasons.append('代理进程未通过存活检查')
        if not local_ws: reasons.append('本地监听检查未通过')
        if not direct and not public_ws: reasons.append('公网 WebSocket 检查未通过')
    labels = {'online':'节点上报正常','offline':'节点心跳停止','degraded':'节点连接异常',
              'network_error':'节点出站异常','repairing':'节点正在自动修复','error':'节点任务异常',
              'waiting':'等待安装','installing':'正在应用配置'}
    value = json.dumps([state, bool(process_alive), bool(local_ws), bool(public_ws) if not direct else None])
    if state == 'offline': value = 'offline'
    observe(node_id,'health',value,labels.get(state,'状态变化')+('：'+'；'.join(reasons) if reasons else ''),
            'warning' if state in ('offline','degraded','network_error','error') or reasons else 'info')


def tick():
    from . import agent
    c = db.get_conn()
    for a in c.execute("SELECT a.* FROM node_agents a JOIN nodes n ON n.id=a.node_id WHERE a.bound=1 AND a.health_mode!='none' AND n.status NOT IN ('removed','uninstalling','pending')").fetchall():
        if a['health_mode']=='monitor':
            from . import monitor
            if not monitor.collector_available():continue
        if agent.age(a['last_report_at']) >= 180:
            health(a['node_id'],'offline',False,False,False)
    c.execute("DELETE FROM node_events WHERE created_at<strftime('%Y-%m-%dT%H:%M:%SZ','now','-90 days')")
    c.commit()


def register(app, require_login):
    @app.get('/api/nodes/{node_id}/events')
    def timeline(node_id:int, request:Request, limit:int=Query(default=50,ge=1,le=100)):
        require_login(request)
        if not db.get_node(node_id):raise HTTPException(404,'节点不存在')
        rows = db.get_conn().execute('SELECT id,created_at,kind,level,message FROM node_events WHERE node_id=? ORDER BY id DESC LIMIT ?', (node_id,limit)).fetchall()
        return {'events':[dict(r) for r in rows], 'retention_days':90}
