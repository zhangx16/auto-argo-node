"""Versioned, transactional additive migrations for existing installations."""
import re
import threading
from . import db

LOCK=threading.RLock()


def apply(component,version,change,force=False):
    with LOCK:
        c=db.get_conn()
        c.execute('CREATE TABLE IF NOT EXISTS schema_migrations(component TEXT NOT NULL,version INTEGER NOT NULL,applied_at TEXT NOT NULL,PRIMARY KEY(component,version))')
        if not force and c.execute('SELECT 1 FROM schema_migrations WHERE component=? AND version=?',(component,version)).fetchone():return False
        c.execute('SAVEPOINT schema_change')
        try:
            change(c)
            c.execute('INSERT OR REPLACE INTO schema_migrations VALUES (?,?,?)',(component,version,db.utcnow()))
            c.execute('RELEASE schema_change');c.commit()
        except Exception:
            c.execute('ROLLBACK TO schema_change');c.execute('RELEASE schema_change');raise
        return True


def columns(table,definitions,version=1):
    if not re.fullmatch('[a-z_][a-z_0-9]*',table):raise ValueError('table')
    def change(c):
        existing={r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
        for name,definition in definitions:
            if not re.fullmatch('[a-z_][a-z_0-9]*',name):raise ValueError('column')
            if name not in existing:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    existing={r['name'] for r in db.get_conn().execute('PRAGMA table_info('+table+')')}
    return apply('columns:'+table,version,change,force=any(name not in existing for name,_ in definitions))


def indexes():
    def change(c):
        for name,table,fields in [('idx_jobs_status_created','agent_jobs','status,created_at'),('idx_agents_probe','node_agents','bound,probe_at'),('idx_ssh_tasks_server_created','ssh_tasks','server_id,created'),('idx_ssh_tasks_due','ssh_tasks','status,next_check')]:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                c.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table}({fields})')
    apply('operations-indexes',1,change)


def status():
    c=db.get_conn()
    return {'migrations':[dict(r) for r in c.execute('SELECT component,version,applied_at FROM schema_migrations ORDER BY component,version')],
      'journal_mode':c.execute('PRAGMA journal_mode').fetchone()[0],
      'busy_timeout_ms':c.execute('PRAGMA busy_timeout').fetchone()[0],
      'database_bytes':db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0,
      'wal_bytes':db.DB_PATH.with_name(db.DB_PATH.name+'-wal').stat().st_size if db.DB_PATH.with_name(db.DB_PATH.name+'-wal').exists() else 0}
