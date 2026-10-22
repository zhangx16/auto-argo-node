"""Agent-managed direct Reality Vision and Hysteria2 nodes."""
import json
import shlex
from pathlib import Path
from urllib.parse import quote, urlencode
from . import db
ROOT=Path(__file__).parent
KINDS=('reality','hysteria2')

def init():
    db.get_conn().execute('CREATE TABLE IF NOT EXISTS direct_node_state(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE, metadata_json TEXT NOT NULL)')
    db.get_conn().commit()

def state(node_id):
    r=db.get_conn().execute('SELECT metadata_json FROM direct_node_state WHERE node_id=?',(node_id,)).fetchone()
    return json.loads(r['metadata_json']) if r else None

def make_link(meta,body):
    host=body.client_address
    if not host:raise ValueError('直连节点需要公网连接地址')
    if ':' in host:host='['+host+']'
    common=f"{quote(meta['uuid'],safe='')}@{host}:{body.client_port}"
    if meta['protocol']=='reality':
        if not meta.get('public_key') or not meta.get('short_id'):raise ValueError('Reality 公钥或 Short ID 缺失')
        query={'security':'reality','encryption':'none','type':'tcp','flow':'xtls-rprx-vision','sni':meta['domain'],'pbk':meta['public_key'],'sid':meta['short_id'],'fp':'chrome'}
        return 'vless://'+common+'?'+urlencode(query)+'#'+quote(body.remark,safe='')
    return 'hysteria2://'+common+'?'+urlencode({'sni':meta['domain'],'insecure':'1','alpn':'h3'})+'#'+quote(body.remark,safe='')

def driver(node_id,config,operation):
    assignments='\n'.join(k+'='+shlex.quote(str(v)) for k,v in {'DIR':f'/usr/local/etc/argo-deploy/node-{node_id}',
        'NODE_ID':node_id,'PORT':config['node_port'],'CREDENTIAL':config['uuid'],'SNI':config['server_name'],
        'KIND':config['protocol'],'OPERATION':operation}.items())
    return "#!/bin/sh\nset -eu\nexec bash <<'DIRECT_DRIVER'\nset -e\numask 077\n"+assignments+'\n'+(ROOT/'node-direct.sh').read_text()+"\nDIRECT_DRIVER\n"

def uninstall(node_id,config,revision):
    template=(ROOT/'node-uninstall.sh').read_text()
    start=template.index('remove_owned() {');end=template.index('if _with_state_lock remove_owned;',start)
    remove='''remove_owned() {
 local tag="panel-__KIND__-__ID__"
 if [ -f "$CONFIG_FILE" ]; then
  jq -e --arg t "$tag" --arg u "$EXPECTED" '[.inbounds[]? | select(.tag==$t) | .users[]? | (.uuid // .password)] | all(.==$u)' "$CONFIG_FILE" >/dev/null || return 1
  _atomic_modify_json "$CONFIG_FILE" 'del(.inbounds[] | select(.tag==$t))' --arg t "$tag" || return 1
 fi
 rm -f "$SINGBOX_DIR/${tag}.pem" "$SINGBOX_DIR/${tag}.key" "$DIR/direct.json"
 if [ -f "$CONFIG_FILE" ] && [ "$(jq '.inbounds | length' "$CONFIG_FILE")" -gt 0 ]; then
  _manage_service restart || return 1
 elif [ -f "$CONFIG_FILE" ]; then
  _manage_service stop || return 1
  if [ "$INIT_SYSTEM" = systemd ]; then systemctl disable sing-box || return 1; else rc-update del sing-box default 2>/dev/null || true; fi
  rm -f /etc/systemd/system/sing-box.service /etc/init.d/sing-box
  if [ "$INIT_SYSTEM" = systemd ]; then systemctl daemon-reload || return 1; fi
  rm -rf "$SINGBOX_DIR"
  rm -f "$SINGBOX_BIN" "$CLOUDFLARED_BIN" /usr/local/bin/sb
 fi
}
'''.replace('__KIND__',config['protocol']).replace('__ID__',str(node_id))
    return (template[:start]+remove+template[end:]).replace('__NODE_ID__',str(node_id)).replace('__UUID__',shlex.quote(config['uuid'])).replace('__REVISION__',str(revision))
