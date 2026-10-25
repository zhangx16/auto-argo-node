"""Read-only Telegram shortcuts, scoped to the configured notification chat."""
import hashlib
import json
import re
import time
from . import db

COMMANDS=[{'command':k,'description':v} for k,v in [
    ('start','打开节点管理助手'),('status','查看节点运行状态'),('traffic','查看流量用量简报'),
    ('nodes','查看节点列表'),('help','查看快捷指令帮助')]]


def sync_menu(cfg=None):
    from . import telemetry
    cfg=cfg or telemetry.config()
    telemetry.telegram('setChatMenuButton',{'menu_button':{'type':'commands'}},cfg)
    scopes=[{'type':'default'}]
    chat=cfg.get('chat_id','')
    if chat:
        scopes.append({'type':'chat','chat_id':chat})
        if int(chat)>0:telemetry.telegram('setChatMenuButton',{'chat_id':chat,'menu_button':{'type':'commands'}},cfg)
    for scope in scopes:
        for language in ('','zh'):
            telemetry.telegram('setMyCommands',{'commands':COMMANDS,'scope':scope,'language_code':language},cfg)
    return {'ok':True}


def recent_chats():
    try:return json.loads(db.get_setting('telegram_recent_chats','[]'))
    except (ValueError,TypeError):return []


def remember_chat(chat):
    if not chat.get('id'):return
    from . import telemetry
    row={'id':str(chat['id']),'name':telemetry.clean(chat.get('title') or chat.get('first_name') or str(chat['id'])),'type':chat.get('type','')}
    rows=[row]+[c for c in recent_chats() if c['id']!=row['id']]
    db.set_setting('telegram_recent_chats',json.dumps(rows[:20]))


def node_text():
    from . import agent,telemetry
    rows=db.get_conn().execute("SELECT n.id,a.health_mode,a.last_report_at,a.health_status,a.probe_status,a.probe_at FROM nodes n JOIN node_agents a ON a.node_id=n.id WHERE a.bound=1 AND n.status NOT IN ('removed','uninstalling','pending') ORDER BY n.id DESC").fetchall()
    lines=['🖥 节点状态一览','']
    for n in rows[:25]:
        fresh=agent.age(n['probe_at'])<600
        if fresh and n['probe_status']=='failed':icon,label='🔴','实际转发未通过'
        elif n['health_mode']!='none' and agent.age(n['last_report_at'])>=180:icon,label='⚪','心跳已过期'
        elif fresh and n['probe_status']=='ok':icon,label='🟢','实际转发通过'
        else:icon,label='🟡','等待新的转发验证'
        lines += [icon+' '+telemetry.node_name(n['id']), '    '+label]
    if not rows:lines.append('尚无已绑定节点')
    if len(rows)>25:lines.append(f'另有 {len(rows)-25} 个节点，请打开面板查看。')
    return '\n'.join(lines+['','🕒 '+telemetry.china_time()])


def command_text(command):
    from . import telemetry
    if command=='traffic':return telemetry.summary_text(),'traffic-module'
    if command in ('status','nodes'):return node_text(),'server-module'
    return telemetry.message_text('👋 节点管理助手' if command=='start' else '📖 快捷指令帮助',hint='查询只向面板已绑定的聊天开放。')+'\n\n/start  打开助手\n/status  节点运行状态\n/nodes  查看节点列表\n/traffic  流量用量简报\n/help  指令说明\n\n点击左侧指令菜单，或直接发送以上命令。','server-module'


def poll(cfg):
    from . import telemetry
    if not cfg.get('token') or not cfg.get('chat_id'):return
    generation=hashlib.sha256(cfg['token'].encode()).hexdigest()[:16]
    saved=json.loads(db.get_setting('telegram_command_cursor','{}'))
    offset=saved.get('offset',0) if saved.get('generation')==generation else 0
    updates=telemetry.telegram('getUpdates',{'offset':offset,'limit':20,'timeout':0,'allowed_updates':['message','my_chat_member']},cfg)
    candidate=None;now=int(time.time())
    for update in updates:
        uid=update.get('update_id')
        if not isinstance(uid,int) or uid<offset:continue
        offset=max(offset,uid+1)
        message=update.get('message') or {};chat=message.get('chat') or (update.get('my_chat_member') or {}).get('chat') or {}
        remember_chat(chat)
        if str(chat.get('id'))!=str(cfg['chat_id']) or message.get('from',{}).get('is_bot'):continue
        if now-int(message.get('date',0))>180:continue
        match=re.match(r'^/(start|status|traffic|nodes|help)(?:@([A-Za-z0-9_]+))?(?:\s|$)',message.get('text',''))
        if not match:continue
        if match.group(2) and match.group(2).lower()!=cfg.get('bot_username','').lower():continue
        candidate=(uid,match.group(1))
    if candidate:
        last=int(db.get_setting('telegram_command_replied_at','0'))
        if now-last>=10:
            uid,command=candidate;text,route=command_text(command)
            telemetry.enqueue(f'command:{generation}:{uid}',text,category='command',route=route,priority=2)
            db.set_setting('telegram_command_replied_at',str(now))
    db.set_setting('telegram_command_cursor',json.dumps({'generation':generation,'offset':offset}))
    db.set_setting('telegram_command_error','')
