"""One encrypted, in-flight configuration checkpoint per node; bounded automatic rollback.

Only client address/port and agent listener edits are managed here. Existing remote
scripts remain responsible for atomic local failure recovery. Cloud/DNS migrations
retain their existing transaction safeguards and are not silently reversed.
"""
import json
from fastapi import HTTPException
from . import db, cloudflare, events

ACTIVE = ('awaiting_node','verifying','rolling_back','verifying_rollback')
LABELS = {'awaiting_node':'等待节点应用新配置','verifying':'正在验证新配置',
          'rolling_back':'新配置验证失败，等待节点恢复原监听配置','verifying_rollback':'正在验证恢复后的配置',
          'succeeded':'新配置验证通过','rolled_back':'已恢复修改前配置并通过验证',
          'rollback_failed':'恢复后仍未通过验证，请人工检查','unverified':'原配置未经测活确认，未启用自动回滚','retry_requested':'用户请求重试，已结束原配置的恢复验证'}


def init():
    db.get_conn().execute('''CREATE TABLE IF NOT EXISTS node_changes(
      node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
      state TEXT NOT NULL, checkpoint TEXT NOT NULL, baseline_ok INTEGER NOT NULL,
      expected_revision INTEGER, target_link TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)''')
    db.get_conn().commit()


def row(node_id):
    r = db.get_conn().execute('SELECT * FROM node_changes WHERE node_id=?',(node_id,)).fetchone()
    return dict(r) if r else None


def public(node_id):
    r = row(node_id)
    return {'change_status':r['state'], 'change_message':LABELS.get(r['state'],''), 'change_at':r['updated_at']} if r else {}


def guard(node_id):
    r = row(node_id)
    if r and r['state'] in ACTIVE:
        raise HTTPException(409,'上一项配置变更正在应用、验证或回滚，请等待完成')


def begin(node_id):
    from . import agent
    guard(node_id)
    n = db.get_node(node_id);a = agent.row(node_id)
    if not n['share_link'] or not a['bound']:return
    dep = db.get_conn().execute('SELECT client_port,ws_path FROM deployments WHERE node_id=?',(node_id,)).fetchone()
    snapshot = {'node':{k:n[k] for k in ('remark','node_port','client_address','share_link','uuid','sni')},
                'desired':json.loads(a['desired_json']), 'deployment':dict(dep)}
    snapshot['desired']['node_port'] = a['actual_port']
    c = db.get_conn()
    c.execute('INSERT INTO node_changes VALUES (?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET state=excluded.state,checkpoint=excluded.checkpoint,baseline_ok=excluded.baseline_ok,expected_revision=NULL,target_link=excluded.target_link,updated_at=excluded.updated_at',
              (node_id,'verifying',cloudflare.encrypt(json.dumps(snapshot)),int(a['probe_status']=='ok' and agent.age(a['probe_at'])<600 and agent.age(a['last_report_at'])<180),None,n['share_link'],db.utcnow()))
    events.add(node_id,'change','已暂存修改前配置；新配置将进行实际转发验证')
    c.commit()


def snapshot(r):
    return json.loads(cloudflare.decrypt(r['checkpoint']))


def state(node_id,value,terminal=False):
    c = db.get_conn()
    c.execute('UPDATE node_changes SET state=?,updated_at=?'+(",checkpoint='',target_link=''" if terminal else '')+' WHERE node_id=?', (value,db.utcnow(),node_id))
    events.add(node_id,'change',LABELS[value], 'warning' if value in ('rolling_back','rollback_failed') else 'info')
    c.commit()


def track(node_id):
    from . import agent
    r = row(node_id)
    if not r or r['state'] not in ACTIVE:return
    a = agent.row(node_id)
    remote = bool(db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND revision=? AND status IN ('queued','running')",(node_id,a['desired_revision'])).fetchone())
    c = db.get_conn()
    c.execute('UPDATE node_changes SET state=?,expected_revision=?,target_link=?,updated_at=? WHERE node_id=?',
              ('awaiting_node' if remote else 'verifying',a['desired_revision'] if remote else None,db.get_node(node_id)['share_link'],db.utcnow(),node_id))
    c.commit()


def restored_parameters(node_id,revision):
    r = row(node_id)
    if r and r['state']=='rolling_back' and r['expected_revision']==revision:
        snap = snapshot(r)
        return dict(remark=snap['node']['remark'],client_address=snap['node']['client_address'],client_port=snap['deployment']['client_port'])
    return {}


def restore_panel(node_id,snap,restore_link=True):
    c = db.get_conn();n = snap['node'];dep = snap['deployment']
    c.execute('UPDATE node_agents SET desired_json=?,last_job_error=\'\' WHERE node_id=?',(json.dumps(snap['desired']),node_id))
    c.execute('UPDATE deployments SET client_port=? WHERE node_id=?',(dep['client_port'],node_id))
    c.execute('UPDATE nodes SET remark=?,client_address=? WHERE id=?',(n['remark'],n['client_address'],node_id))
    if restore_link:
        # Preserve fresh tunnel metadata: a temporary tunnel may legitimately rotate meanwhile.
        from . import agent, deploy, direct_nodes
        current = db.get_node(node_id)
        body = agent.AgentEditBody(remark=n['remark'],node_port=current['node_port'],client_address=n['client_address'],client_port=dep['client_port'])
        if snap['desired'].get('protocol') in direct_nodes.KINDS:
            link = direct_nodes.make_link(direct_nodes.state(node_id),body)
        else:
            path = c.execute('SELECT ws_path FROM deployments WHERE node_id=?',(node_id,)).fetchone()['ws_path']
            link = deploy.make_link({'domain':current['sni'],'uuid':current['uuid'],'path':path},body)
        c.execute('UPDATE nodes SET share_link=? WHERE id=?',(link,node_id))
    c.execute("UPDATE node_agents SET probe_status='unknown',probe_at='',probe_error='',latency_ms=NULL WHERE node_id=?",(node_id,))
    c.commit()


def job_result(node_id,revision,ok):
    r = row(node_id)
    if not r or r['state'] not in ('awaiting_node','rolling_back') or r['expected_revision']!=revision:return False
    if not ok:
        if r['state']=='rolling_back':
            state(node_id,'rollback_failed',True);return False
        # The remote transaction already restored its local files. Restore matching panel parameters.
        restore_panel(node_id,snapshot(r))
        state(node_id,'verifying_rollback')
    else:
        state(node_id,'verifying_rollback' if r['state']=='rolling_back' else 'verifying')
    c = db.get_conn()
    c.execute('UPDATE node_changes SET target_link=? WHERE node_id=?',(db.get_node(node_id)['share_link'],node_id));c.commit()
    return True


def needs_confirmation(node_id,link):
    r = row(node_id)
    return bool(r and r['state'] in ('verifying','verifying_rollback') and r['target_link']==link)


def probe_result(node_id,link,ok):
    from . import agent
    r = row(node_id)
    if not r or r['state'] not in ('verifying','verifying_rollback') or r['target_link']!=link:return False
    n = db.get_node(node_id)
    if not n or n['share_link']!=link or n['status']=='uninstalling':return False
    if ok:
        rollback = r['state']=='verifying_rollback'
        state(node_id,'rolled_back' if rollback else 'succeeded',True)
        if rollback:
            c=db.get_conn();c.execute("UPDATE node_agents SET last_job_error='' WHERE node_id=?",(node_id,))
            c.execute("UPDATE nodes SET last_error='',status='online' WHERE id=?",(node_id,));c.commit()
        return False
    if r['state']=='verifying_rollback':
        state(node_id,'rollback_failed',True);return False
    if not r['baseline_ok']:
        state(node_id,'unverified',True);return False
    snap = snapshot(r);a = agent.row(node_id)
    if snap['node']['node_port'] != a['actual_port']:
        # Reapply the old listener using a new monotonic revision; never replay an acknowledged job.
        config = snap['desired'] | {'remark':snap['node']['remark'],'client_address':snap['node']['client_address'],'client_port':snap['deployment']['client_port']}
        state(node_id,'rolling_back')
        revision = agent.enqueue(node_id,'update',config)
        c = db.get_conn();c.execute('UPDATE node_changes SET expected_revision=? WHERE node_id=?',(revision,node_id));c.commit()
        return False
    restore_panel(node_id,snap)
    state(node_id,'verifying_rollback')
    c=db.get_conn();c.execute('UPDATE node_changes SET target_link=? WHERE node_id=?',(db.get_node(node_id)['share_link'],node_id));c.commit()
    return True


def retry_config(node_id):
    r = row(node_id)
    if not r or r['state'] not in ('rolled_back','rollback_failed','retry_requested'):return None
    job = db.get_conn().execute("SELECT config_json FROM agent_jobs WHERE node_id=? AND status='failed' ORDER BY revision DESC LIMIT 1",(node_id,)).fetchone()
    return json.loads(job['config_json']) if job else None


def prepare_retry(node_id):
    r = row(node_id)
    if r and r['state']=='verifying_rollback':
        failed = db.get_conn().execute("SELECT 1 FROM agent_jobs WHERE node_id=? AND revision=? AND status='failed'",(node_id,r['expected_revision'])).fetchone()
        if failed:
            state(node_id,'retry_requested',True)
            return bool(r['baseline_ok'])
    return False


def retarget(node_id):
    r = row(node_id)
    if not r or r['state'] not in ('verifying','verifying_rollback'):return
    link = db.get_node(node_id)['share_link']
    if link != r['target_link']:
        c = db.get_conn()
        c.execute('UPDATE node_changes SET target_link=?,updated_at=? WHERE node_id=?',(link,db.utcnow(),node_id))
        c.commit()
