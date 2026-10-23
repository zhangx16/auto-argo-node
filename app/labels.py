"""Owner-controlled grouping metadata; never rewrites connection names or credentials."""
import json
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from . import db

Kind=Literal['managed','external']

class LabelBody(BaseModel):
    group_name:str=Field(default='',max_length=40)
    tags:list[str]=Field(default_factory=list,max_length=12)
    @field_validator('group_name')
    @classmethod
    def group_valid(cls,value):return clean(value,40)
    @field_validator('tags')
    @classmethod
    def tags_valid(cls,values):return list(dict.fromkeys(clean(v,24) for v in values if v.strip()))

def clean(value,limit):
    value=value.strip()
    if len(value)>limit or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('分组或标签包含无效字符或过长')
    return value

def init():
    c=db.get_conn()
    for kind,parent in [('managed','nodes'),('external','external_nodes')]:
        c.execute(f'''CREATE TABLE IF NOT EXISTS {kind}_labels(node_id INTEGER PRIMARY KEY REFERENCES {parent}(id) ON DELETE CASCADE,
          group_name TEXT NOT NULL DEFAULT '',tags_json TEXT NOT NULL DEFAULT '[]')''')
    c.commit()

def enrich(node,kind='managed'):
    c=db.get_conn()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(kind+'_labels',)).fetchone():return dict(node,group_name='',tags=[])
    row=c.execute(f'SELECT group_name,tags_json FROM {kind}_labels WHERE node_id=?',(node['id'],)).fetchone()
    return dict(node,group_name=row['group_name'] if row else '',tags=json.loads(row['tags_json']) if row else [])

def register(app,owner):
    @app.get('/api/labels')
    def catalog(request:Request):
        owner(request);groups=set();tags=set()
        for kind in ('managed','external'):
            for row in db.get_conn().execute(f'SELECT group_name,tags_json FROM {kind}_labels'):
                if row['group_name']:groups.add(row['group_name'])
                tags.update(json.loads(row['tags_json']))
        return {'groups':sorted(groups),'tags':sorted(tags)}
    @app.put('/api/labels/{kind}/{node_id}')
    def save(kind:Kind,node_id:int,body:LabelBody,request:Request):
        owner(request)
        c=db.get_conn();parent='nodes' if kind=='managed' else 'external_nodes'
        if not c.execute(f'SELECT 1 FROM {parent} WHERE id=?',(node_id,)).fetchone():raise HTTPException(404,'节点不存在')
        c.execute(f'INSERT INTO {kind}_labels VALUES (?,?,?) ON CONFLICT(node_id) DO UPDATE SET group_name=excluded.group_name,tags_json=excluded.tags_json',
            (node_id,body.group_name,json.dumps(body.tags,ensure_ascii=False)));c.commit()
        return {'group_name':body.group_name,'tags':body.tags}
