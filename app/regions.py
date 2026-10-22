"""Local, mmap-backed country lookup. No IPs are sent to an external lookup API."""
import ipaddress
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from fastapi import HTTPException, Request
from pydantic import BaseModel, field_validator
from . import db

ROOT=Path(__file__).parent
COUNTRIES=json.loads((ROOT/'countries.json').read_text())
READER=None
READER_LOCK=threading.Lock()

class RegionBody(BaseModel):
    country_code: str | None=None
    @field_validator('country_code')
    @classmethod
    def country_valid(cls,v):
        if v is None:return None
        v=v.strip().upper()
        if v not in COUNTRIES:raise ValueError('请选择有效国家/地区，或使用自动识别')
        return v


def init():
    c=db.get_conn()
    for table,parent in [('node_regions','nodes'),('external_regions','external_nodes')]:
        c.execute(f'''CREATE TABLE IF NOT EXISTS {table}(node_id INTEGER PRIMARY KEY REFERENCES {parent}(id) ON DELETE CASCADE,
          manual_code TEXT,auto_code TEXT NOT NULL DEFAULT '',ip TEXT NOT NULL DEFAULT '',checked INTEGER NOT NULL DEFAULT 0)''')
    c.commit()


def table_for(kind):
    if kind not in ('managed','external'):raise HTTPException(422,'无效节点类型')
    return 'node_regions' if kind=='managed' else 'external_regions'


def flag(code):
    if not code or code=='ZZ':return ''
    return ''.join(chr(127397+ord(c)) for c in code)


def display_name(name,code):
    if not code:return name
    base=re.sub(r'^[\U0001F1E6-\U0001F1FF]{2}\s*','',str(name)).strip()
    return base if code=='ZZ' else flag(code)+' '+base


def enrich(node,kind='managed'):
    n=dict(node);table=table_for(kind)
    # Legacy unit tests may initialize only the SSH schema.
    if not db.get_conn().execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():r=None
    else:r=db.get_conn().execute(f'SELECT * FROM {table} WHERE node_id=?',(n['id'],)).fetchone()
    code=(r['manual_code'] if r and r['manual_code'] is not None else r['auto_code'] if r else '')
    n.update(country_code=code,country_flag=flag(code),country_source='manual' if r and r['manual_code'] is not None else 'auto',
             country_auto=r['auto_code'] if r else '',country_ip=r['ip'] if r else '',country_checked=r['checked'] if r else 0,
             display_name=display_name(n.get('remark',n.get('name','')),code))
    return n


def lookup(ip):
    global READER
    try:
        address=ipaddress.ip_address(ip)
        if not address.is_global:return ''
        with READER_LOCK:
            if READER is None:
                import maxminddb
                path=os.environ.get('GEOIP_PATH',str(ROOT/'vendor/country.mmdb'))
                READER=maxminddb.open_database(path) # AUTO uses mmap, never Mode.MEMORY.
            item=READER.get(str(address)) or {}
        code=item.get('country',{}).get('iso_code','').upper()
        return code if code in COUNTRIES else ''
    except (OSError,ValueError,ImportError):return ''


def _refresh_pending(limit=12):
    c=db.get_conn();now=int(time.time());done=0
    # Managed nodes use their reported public host IP, never the preferred CF IP.
    for kind,parent,col in [('managed','nodes','host_ip'),('external','external_nodes','server')]:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(parent,)).fetchone():continue
        table=table_for(kind)
        rows=c.execute(f'''SELECT n.id,n.{col} address,r.ip,r.checked FROM {parent} n
          LEFT JOIN {table} r ON r.node_id=n.id WHERE r.node_id IS NULL OR r.checked<? OR (?='managed' AND n.{col}<>r.ip)
          ORDER BY coalesce(r.checked,0),n.id LIMIT ?''',(now-86400,kind,limit-done)).fetchall()
        for n in rows:
            ip=str(n['address'] or '').strip();code=''
            try:
                address=ipaddress.ip_address(ip)
                if address.is_global:code=lookup(str(address))
            except ValueError:
                # Resolve at most one external hostname per tick, outside HTTP handlers.
                if kind=='external' and ip and done<limit:
                    try:
                        candidates=socket.getaddrinfo(ip,None,type=socket.SOCK_STREAM)
                        ip=next(str(x[4][0]) for x in candidates if ipaddress.ip_address(x[4][0]).is_global)
                        code=lookup(ip)
                    except (OSError,ValueError,StopIteration):ip=''
                    done=limit-1
                else:ip=''
            c.execute(f'''INSERT INTO {table}(node_id,auto_code,ip,checked) VALUES (?,?,?,?)
              ON CONFLICT(node_id) DO UPDATE SET auto_code=excluded.auto_code,ip=excluded.ip,checked=excluded.checked''',(n['id'],code,ip,now))
            done+=1
            if done>=limit:break
        if done>=limit:break
    c.commit()


def refresh_pending(limit=12):
    try:_refresh_pending(limit)
    except Exception:
        db.get_conn().rollback()
        raise


def register(app,owner):
    @app.get('/api/regions')
    def list_regions(request:Request):
        owner(request)
        return {'countries':COUNTRIES,'attribution':'IP Geolocation by DB-IP','source':'https://db-ip.com','release':'2026-10'}
    @app.put('/api/regions/{kind}/{node_id}')
    def set_region(kind:str,node_id:int,body:RegionBody,request:Request):
        owner(request);table=table_for(kind);parent='nodes' if kind=='managed' else 'external_nodes'
        n=db.get_conn().execute(f'SELECT * FROM {parent} WHERE id=?',(node_id,)).fetchone()
        if not n:raise HTTPException(404,'节点不存在')
        c=db.get_conn();c.execute(f'INSERT INTO {table}(node_id,manual_code) VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET manual_code=excluded.manual_code',(node_id,body.country_code));c.commit()
        return enrich(dict(n),kind)
