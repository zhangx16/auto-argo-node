"""Bounded, local subscription references. No subscription URLs are fetched."""
import json
from fastapi import HTTPException
from . import db, external_nodes, proxy_chains, subscription_health

MAX_DEPTH=8
MAX_SOURCES=64
MAX_NODES=1000
MAX_CHAINS=120


def init():
    db.get_conn().executescript('''CREATE TABLE IF NOT EXISTS subscription_includes(
      subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
      source_id INTEGER NOT NULL, source_name TEXT NOT NULL, position INTEGER NOT NULL,
      PRIMARY KEY(subscription_id,source_id));''')
    from . import schema
    schema.columns('subscriptions',[('usage_source_id','INTEGER NOT NULL DEFAULT 0')],version=1)
    db.get_conn().commit()


def references(subscription_id):
    return [dict(r) for r in db.get_conn().execute('SELECT source_id,source_name FROM subscription_includes WHERE subscription_id=? ORDER BY position',(subscription_id,))]


def validate(subscription_id,source_ids,usage_source_id):
    if usage_source_id and usage_source_id not in source_ids:
        raise HTTPException(422,'客户端用量来源必须是已引用的订阅')
    rows={r['id']:dict(r) for r in db.get_conn().execute('SELECT * FROM subscriptions')}
    # Unchanged missing references remain visible after deleting a source; new ones cannot be created.
    old={r['source_id'] for r in references(subscription_id)} if subscription_id else set()
    if any(s not in rows and s not in old for s in source_ids):raise HTTPException(422,'选择的原订阅不存在')
    graph={}
    for r in db.get_conn().execute('SELECT subscription_id,source_id FROM subscription_includes'):
        graph.setdefault(r['subscription_id'],[]).append(r['source_id'])
    root=subscription_id or -1;graph[root]=source_ids
    # Check all ancestors as well: adding an edge must not overgrow existing parents.
    for start in graph:
        visited=set();best_depth={}
        def walk(sid,path):
            if sid in path:raise HTTPException(422,'订阅不能引用自身或形成循环引用')
            if len(path)>MAX_DEPTH:raise HTTPException(422,'订阅引用最多 8 层')
            if best_depth.get(sid,-1)>=len(path):return
            best_depth[sid]=len(path)
            visited.add(sid)
            if len(visited)>MAX_SOURCES:raise HTTPException(422,'一个汇总订阅最多关联 64 个订阅')
            for child in graph.get(sid,[]):walk(child,path+(sid,))
        walk(start,())
    return rows


def save(subscription_id,source_ids,rows):
    c=db.get_conn();old={r['source_id']:r['source_name'] for r in references(subscription_id)}
    c.execute('DELETE FROM subscription_includes WHERE subscription_id=?',(subscription_id,))
    c.executemany('INSERT INTO subscription_includes VALUES (?,?,?,?)',[(subscription_id,sid,rows[sid]['name'] if sid in rows else old.get(sid,'已删除的订阅'),pos) for pos,sid in enumerate(source_ids)])


def resolve(subscription):
    from . import subscriptions as subs
    cache={};visiting=set();seen=set()
    def visit(row,depth):
        sid=row['id']
        if sid in visiting or depth>MAX_DEPTH:raise HTTPException(409,'订阅引用循环或层级超限，请编辑订阅修正')
        if sid in cache:return cache[sid]
        seen.add(sid)
        if len(seen)>MAX_SOURCES:raise HTTPException(409,'汇总订阅关联数量超限')
        visiting.add(sid)
        managed=subs.member_nodes(sid);imported=subs.external_members(sid)
        isolated=subscription_health.isolated_ids(sid)
        members={('managed',n['id']):n for n in managed if subs.eligible(n) and n['id'] not in isolated}
        members.update({('external',n['id']):n for n in imported if external_nodes.available(n)})
        configured=proxy_chains.configured(row)
        chains=subscription_health.chains(row,configured)
        errors=proxy_chains.errors(chains,set(members))
        standalone=set(members) if not row.get('chain_only') else set()
        plan={'members':members,'standalone':standalone,'chains':{(sid,i):c for i,c in enumerate(chains)},
          'origins':{sid:(row,managed,imported)},'states':{},'errors':errors,'has_chains':bool(configured)}
        for ref in references(sid):
            source_id=ref['source_id'];source=db.get_conn().execute('SELECT * FROM subscriptions WHERE id=?',(source_id,)).fetchone()
            name=source['name'] if source else ref['source_name']
            state='missing' if not source else 'disabled' if not source['enabled'] else 'expired' if subs.expired(dict(source)) else 'ok'
            if state=='ok':
                child=visit(dict(source),depth+1)
                if child['errors']:state='invalid'
                elif len(set(plan['members'])|set(child['members']))>MAX_NODES or len(set(plan['chains'])|set(child['chains']))>MAX_CHAINS:
                    state='limit'
                else:
                    for key in ('members','chains','origins'):plan[key].update(child[key])
                    plan['standalone'].update(child['standalone']);plan['states'].update(child['states'])
                    plan['has_chains']|=child['has_chains']
            plan['states'][source_id]={'subscription_id':source_id,'name':name,'status':state}
        if len(plan['members'])>MAX_NODES or len(plan['chains'])>MAX_CHAINS:raise HTTPException(409,'汇总后最多 1000 个节点、120 条链路，请减少引用')
        visiting.remove(sid);cache[sid]=plan;return plan
    plan=visit(subscription,0)
    # A quarantine applies on its own source branch. Inherited chains never become standalone nodes.
    needed=set(plan['standalone'])|{(h['kind'],h['id']) for c in plan['chains'].values() for h in c['hops']}
    members={ref:n for ref,n in plan['members'].items() if ref in needed}
    aliases={};identities={}
    for ref,n in members.items():
        uri=n['share_link'] if ref[0]=='managed' else n['uri']
        try:identity=external_nodes.fingerprint(external_nodes.parse_uri(uri))
        except (ValueError,TypeError):identity=ref
        aliases[ref]=identities.setdefault(identity,ref)
    final_members={ref:n for ref,n in members.items() if aliases[ref]==ref}
    chains=[];used_names=set()
    for (sid,index),chain in plan['chains'].items():
        name=chain['name'] if sid==subscription['id'] else plan['origins'][sid][0]['name']+' · '+chain['name']
        while name in used_names:name+=' _'
        used_names.add(name)
        hops=[{'kind':aliases.get((h['kind'],h['id']),(h['kind'],h['id']))[0],'id':aliases.get((h['kind'],h['id']),(h['kind'],h['id']))[1]} for h in chain['hops']]
        chains.append(dict(chain,name=name,hops=hops))
    return dict(plan,members=final_members,standalone={aliases[r] for r in plan['standalone'] if r in aliases},chains=chains,states=list(plan['states'].values()))
