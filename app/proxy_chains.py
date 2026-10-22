"""Client-side ordered proxy chains; never mutate server-side node configuration."""
import json
from typing import Annotated,Literal
from pydantic import BaseModel,Field,field_validator
from fastapi import HTTPException
from .node_formats import clash_proxy,singbox_outbound

class Hop(BaseModel):
    kind: Literal['managed','external']
    id: Annotated[int,Field(ge=1)]
    model_config={'extra':'forbid'}

class Chain(BaseModel):
    name: str=Field(min_length=1,max_length=80)
    hops: list[Hop]=Field(min_length=2,max_length=5)
    model_config={'extra':'forbid'}

    @field_validator('name')
    @classmethod
    def clean_name(cls,v):
        v=v.strip()
        if not v or any(ord(c)<32 or ord(c)==127 for c in v):raise ValueError('请输入有效的链路名称')
        return v

    @field_validator('hops')
    @classmethod
    def no_repeat(cls,hops):
        refs=[(h.kind,h.id) for h in hops]
        if len(set(refs))!=len(refs):raise ValueError('同一链路不能重复使用同一节点')
        return hops


def configured(subscription):
    return json.loads(subscription.get('chains_json','[]'))


def validate(chains,node_ids,external_ids,chain_only):
    refs={('managed',i) for i in node_ids}|{('external',i) for i in external_ids}
    if chain_only and not chains:raise HTTPException(422,'仅导出链式节点时，至少配置一条链路')
    if len({chain.name for chain in chains})!=len(chains):raise HTTPException(422,'链路名称不能重复')
    for chain in chains:
        if any((hop.kind,hop.id) not in refs for hop in chain.hops):
            raise HTTPException(409,'链路「'+chain.name+'」含未勾选节点，请先选择全部入口、中转和出口节点')


def errors(chains,available):
    return ['链路「'+chain['name']+'」有节点已删除、停用或卸载，请重新选择'
            for chain in chains if any((h['kind'],h['id']) not in available for h in chain['hops'])]


def expand(chains,parsed_by_ref,include_standalone):
    used={'DIRECT','direct','proxy','节点选择'}|{p['name'] for p in parsed_by_ref.values()}
    def unique(name):
        while name in used:name+=' _'
        used.add(name);return name
    copies=[];names=[]
    for chain in chains:
        previous=None
        for i,ref in enumerate(chain['hops']):
            p=parsed_by_ref.get((ref['kind'],ref['id']))
            if p is None:raise HTTPException(409,'链路「'+chain['name']+'」不完整，已停止导出，避免绕过代理链')
            final=i==len(chain['hops'])-1
            name=unique('链式 · '+chain['name']+('' if final else f" / {i+1} · "+p['name']))
            clone=dict(p,name=name)
            copies.append((clone,previous))
            previous=name
        names.append(previous)
    ordinary=list(parsed_by_ref.values()) if include_standalone else []
    return [(p,None) for p in ordinary]+copies,names+[p['name'] for p in ordinary]


def proxies(expanded,fmt):
    result=[]
    for p,previous in expanded:
        item=clash_proxy(p) if fmt=='clash' else singbox_outbound(p)
        if previous:item['dialer-proxy' if fmt=='clash' else 'detour']=previous
        result.append(item)
    return result
