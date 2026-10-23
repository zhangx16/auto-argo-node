"""Bounded host samples supplied by the existing cron worker, never a new daemon."""
import json
import time
from fastapi import Request
from pydantic import BaseModel, Field, ConfigDict
from . import db

class Sample(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    sampled_at: int = Field(ge=0, le=9999999999)
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    cpu_cores: int = Field(ge=0, le=65536)
    load1: float = Field(ge=0, le=1000000)
    memory_total: int = Field(ge=0, le=2**60)
    memory_used: int = Field(ge=0, le=2**60)
    swap_total: int = Field(ge=0, le=2**60)
    swap_used: int = Field(ge=0, le=2**60)
    disk_total: int = Field(ge=0, le=2**60)
    disk_used: int = Field(ge=0, le=2**60)
    uptime_seconds: int = Field(ge=0, le=2**53)
    singbox_rss: int = Field(ge=0, le=2**60)
    cloudflared_rss: int = Field(ge=0, le=2**60)
    singbox_processes: int = Field(ge=0, le=100000)
    cloudflared_processes: int = Field(ge=0, le=100000)


def init():
    c = db.get_conn()
    c.executescript('''CREATE TABLE IF NOT EXISTS node_resource_latest (
      node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      received_at INTEGER NOT NULL, sample TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS node_resource_history (
      node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
      bucket INTEGER NOT NULL, sample TEXT NOT NULL, PRIMARY KEY(node_id,bucket));
    CREATE INDEX IF NOT EXISTS node_resource_history_bucket ON node_resource_history(bucket);''')
    c.commit()


def record(node_id, sample):
    if sample is None:return
    now = int(time.time()); c = db.get_conn()
    data = sample.model_dump()
    # Host clock is informational. Retention and freshness use panel receipt time.
    data['received_at'] = now
    encoded = json.dumps(data, separators=(',', ':'))
    c.execute('INSERT OR REPLACE INTO node_resource_latest VALUES (?,?,?)', (node_id, now, encoded))
    c.execute('INSERT OR REPLACE INTO node_resource_history VALUES (?,?,?)', (node_id, now//300, encoded))
    c.execute('DELETE FROM node_resource_history WHERE bucket<=?', (now//300-288,))


def snapshot():
    now = int(time.time())
    result = []
    for row in db.get_conn().execute('''SELECT n.id,n.remark,a.bound,a.agent_version,r.received_at,r.sample
       FROM node_agents a JOIN nodes n ON n.id=a.node_id
       LEFT JOIN node_resource_latest r ON r.node_id=a.node_id ORDER BY n.id'''):
        data = json.loads(row['sample']) if row['sample'] else None
        result.append(dict(node_id=row['id'], name=row['remark'], bound=bool(row['bound']),
                           agent_version=row['agent_version'], stale=not data or now-row['received_at']>180,
                           sample=data, diagnostic_command=f"sh /usr/local/lib/argo-deploy/diagnose.sh {row['id']}"))
    return {'nodes':result, 'retention_hours':24, 'history_interval_seconds':300}


def register(app, owner):
    @app.get('/api/resources')
    def listing(request: Request):
        owner(request)
        return snapshot()

    @app.get('/api/resources/{node_id}/history')
    def history(node_id: int, request: Request):
        owner(request)
        if not db.get_node(node_id):
            from fastapi import HTTPException
            raise HTTPException(404, '节点不存在')
        rows = db.get_conn().execute('SELECT sample FROM node_resource_history WHERE node_id=? AND bucket>? ORDER BY bucket', (node_id, int(time.time())//300-288))
        return {'samples':[json.loads(r['sample']) for r in rows]}
