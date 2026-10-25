"""Pilot named-tunnel migration of enrolled nodes, preserving local inbound identity."""
import shlex
from pathlib import Path

ROOT = Path(__file__).parent


def upgrade(node_id, port, protocol='vless-ws-argo', expected_hashes=None):
    checks='\n'.join("printf '%s  %s\\n' "+shlex.quote(digest)+" \"$TMP/"+name+"\" | sha256sum -c -" for name,digest in (expected_hashes or {}).items())
    return '''#!/bin/sh
set -eu
umask 077
DIR=/usr/local/etc/argo-deploy/node-'''+str(node_id)+'''
BASE=/usr/local/lib/argo-deploy
PANEL=$(jq -r '.panel' "$DIR/settings.json")
TMP=$(mktemp -d "$DIR/upgrade.XXXXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
for name in agent.sh repair.sh library.sh fixed.sh traffic.sh resources.sh diagnose.sh core.sh; do
 curl --config "$DIR/curl.conf" -fsS "$PANEL/agent/assets/$name" -o "$TMP/$name"
 chmod 700 "$TMP/$name"
done
'''+checks+'''
sh -n "$TMP/agent.sh"
sh -n "$TMP/traffic.sh"
sh -n "$TMP/resources.sh"
sh -n "$TMP/diagnose.sh"
bash -n "$TMP/repair.sh"
bash -n "$TMP/library.sh"
bash -n "$TMP/fixed.sh"
# Keep one pre-migration management implementation for local recovery.
BACKUP=/usr/local/etc/argo-deploy/management-rollback
if [ ! -d "$BACKUP" ];then
 mkdir -p "$BACKUP";chmod 700 "$BACKUP"
 for name in agent.sh repair.sh library.sh fixed.sh traffic.sh resources.sh diagnose.sh;do [ ! -f "$BASE/$name" ] || cp -p "$BASE/$name" "$BACKUP/$name";done
fi
for name in repair.sh library.sh fixed.sh traffic.sh resources.sh diagnose.sh core.sh agent.sh; do mv "$TMP/$name" "$BASE/$name"; done
# Installing nft is optional; never changes existing firewall services.
if ! command -v nft >/dev/null 2>&1;then
 if command -v apk >/dev/null;then apk add --no-cache nftables || true;else apt-get install -y --no-install-recommends nftables || true;fi
fi
printf 'SB_WEB_RESULT='
'''+('cat "$DIR/direct.json"' if protocol in ('reality','hysteria2') else 'jq -c --arg t argo-vless-ws-'+str(port)+" '.[$t]' /usr/local/etc/sing-box/argo_metadata.json")+'''

'''


def migrate(node_id, config):
    constants='\n'.join(k+'='+shlex.quote(str(v)) for k,v in {
        'DIR':f'/usr/local/etc/argo-deploy/node-{node_id}', 'PORT':config['node_port'],
        'EXPECTED':config['uuid'], 'EXPECTED_PATH':config['ws_path']}.items())
    return '''#!/bin/sh
set -eu
exec bash <<'FIXED_MIGRATION'
set -e
umask 077
'''+constants+'''
BASE=/usr/local/lib/argo-deploy
if source "$BASE/library.sh" agent-library; then :; else exit 1; fi
source "$BASE/fixed.sh"
SELF_SCRIPT_PATH=/usr/local/bin/sb
_detect_init_system
exec 7>"$RUNTIME_DIR/argo-keepalive.lock"
flock 7
migrate_fixed() {
 local tag="argo-vless-ws-${PORT}" old_pid new_pid pidfile logfile stagepid stagelog
 local metabak yamlbak sbbackup committed=0
 jq -e --arg t "$tag" --arg u "$EXPECTED" --arg p "$EXPECTED_PATH" \
  '.[$t] | select(.uuid==$u and .path==$p and .protocol=="vless-ws")' "$ARGO_METADATA_FILE" >/dev/null || return 1
 jq -e --arg t "$tag" --arg u "$EXPECTED" --argjson p "$PORT" \
  '.inbounds[] | select(.tag==$t and .listen_port==$p) | .users[] | select(.uuid==$u)' "$CONFIG_FILE" >/dev/null || return 1
 fixed_credentials || return 1
 pidfile=$(_argo_pid_file "$PORT");logfile=$(_argo_log_file "$PORT")
 # Lost acknowledgements are safe: reuse the already committed connector.
 if jq -e --arg t "$tag" --arg h "$FIXED_HOST" '.[$t] | select(.manager=="argo-deploy-fixed" and .domain==$h)' "$ARGO_METADATA_FILE" >/dev/null && _is_pid_file_running_cmd "$pidfile" "$DIR/tunnel-token"; then
  fixed_wait "$EXPECTED_PATH" || return 1
  printf 'SB_WEB_RESULT=';jq -c --arg t "$tag" '.[$t]' "$ARGO_METADATA_FILE"
  return 0
 fi
 stagepid="$DIR/fixed-stage.pid";stagelog="$DIR/fixed-stage.log"
 if [ -s "$stagepid" ]; then fixed_stop_pid "$(cat "$stagepid")" || return 1; fi
 old_pid=$(cat "$pidfile" 2>/dev/null || true)
 metabak=$(mktemp "$DIR/meta-backup.XXXXXXXX");yamlbak=$(mktemp "$DIR/yaml-backup.XXXXXXXX");sbbackup=$(mktemp "$DIR/sb-backup.XXXXXXXX")
 cp "$ARGO_METADATA_FILE" "$metabak" || return 1
 [ ! -f "$CLASH_YAML_FILE" ] || cp "$CLASH_YAML_FILE" "$yamlbak" || return 1
 cp /usr/local/bin/sb "$sbbackup" || return 1
 rollback_fixed() {
  local code=$?
  if [ "$committed" = 0 ]; then
   [ ! -s "$stagepid" ] || fixed_stop_pid "$(cat "$stagepid")" || true
   cp "$metabak" "$ARGO_METADATA_FILE"
   [ ! -s "$yamlbak" ] || cp "$yamlbak" "$CLASH_YAML_FILE"
   cp "$sbbackup" /usr/local/bin/sb
  fi
  rm -f "$metabak" "$yamlbak" "$sbbackup" "$stagepid" "$stagelog"
  return "$code"
 }
 trap rollback_fixed RETURN
 fixed_launch "$stagepid" "$stagelog" || return 1
 fixed_wait "$EXPECTED_PATH" || return 1
 # The interactive watchdog must skip agent-owned fixed tunnels.
 { cat "$BASE/library.sh"; printf '\\nmain\\n'; } > /usr/local/bin/sb.new || return 1
 chmod 700 /usr/local/bin/sb.new;mv /usr/local/bin/sb.new /usr/local/bin/sb || return 1
 _atomic_modify_json "$ARGO_METADATA_FILE" '.[$t] |= (.domain=$h | .type="fixed" | .manager="argo-deploy-fixed" | .token="")' --arg t "$tag" --arg h "$FIXED_HOST" || return 1
 if [ -s "$CLASH_YAML_FILE" ]; then
  export NODE_DOMAIN="$FIXED_HOST" NODE_NAME
  NODE_NAME=$(jq -r --arg t "$tag" '.[$t].name' "$ARGO_METADATA_FILE")
  _atomic_modify_yaml "$CLASH_YAML_FILE" '(.proxies[] | select(.name==env(NODE_NAME))) |= (.server=env(NODE_DOMAIN) | .servername=env(NODE_DOMAIN) | .["ws-opts"].headers.Host=env(NODE_DOMAIN))' || return 1
 fi
 # All fallible setup is complete before the old connector is stopped.
 mv "$stagepid" "$pidfile" || return 1
 mv "$stagelog" "$logfile" || return 1
 committed=1
 fixed_stop_pid "$old_pid" || true
 trap - RETURN
 rm -f "$metabak" "$yamlbak" "$sbbackup"
 printf 'SB_WEB_RESULT=';jq -c --arg t "$tag" '.[$t]' "$ARGO_METADATA_FILE"
}
if _with_state_lock migrate_fixed; then exit 0; else exit 1; fi
FIXED_MIGRATION
'''


def install(node_id, config, name):
    constants='\n'.join(k+'='+shlex.quote(str(v)) for k,v in {
        'DIR':f'/usr/local/etc/argo-deploy/node-{node_id}', 'PORT':config['node_port'],
        'EXPECTED':config['uuid'], 'WS':config['ws_path'], 'NAME':name}.items())
    return """#!/bin/sh
set -eu
exec bash <<'FIXED_INSTALL'
set -e
umask 077
"""+constants+"""
BASE=/usr/local/lib/argo-deploy
if source "$BASE/library.sh" agent-library; then :; else exit 1; fi
source "$BASE/fixed.sh"
SELF_SCRIPT_PATH=/usr/local/bin/sb
SCRIPT_DIR=/usr/local/bin
_detect_init_system
case "$INIT_SYSTEM" in systemd|openrc) ;; *) echo '缺少 systemd/OpenRC'; exit 1;; esac
run_step() { if "$@"; then return 0; else echo "步骤失败：$1"; return 1; fi; }
run_step _install_dependencies
if [ ! -x "$SINGBOX_BIN" ]; then
 run_step _do_update_singbox latest
else
 run_step _initialize_config_files
 run_step _init_relay_config
 run_step _ensure_relay_config
 [ -f "$SERVICE_FILE" ] || run_step _create_service_files
fi
run_step _install_cloudflared
run_step _prepare_argo_runtime_dir
exec 7>"$RUNTIME_DIR/argo-keepalive.lock"
flock 7
install_fixed() {
 local tag="argo-vless-ws-${PORT}" pidfile logfile snapshot committed=0 oldpid
 pidfile=$(_argo_pid_file "$PORT");logfile=$(_argo_log_file "$PORT")
 [ -f "$ARGO_METADATA_FILE" ] || printf '{}\n' > "$ARGO_METADATA_FILE" || return 1
 if jq -e --arg t "$tag" 'has($t)' "$ARGO_METADATA_FILE" >/dev/null; then
  jq -e --arg t "$tag" --arg u "$EXPECTED" '.[$t] | select(.uuid==$u and .manager=="argo-deploy-fixed")' "$ARGO_METADATA_FILE" >/dev/null || { echo '该端口已有其他节点'; return 1; }
 elif _check_port_conflict "$PORT" tcp; then
  echo '监听端口已占用'; return 1
 fi
 fixed_credentials || return 1
 if _is_pid_file_running_cmd "$pidfile" "$DIR/tunnel-token" && fixed_ws "https://${FIXED_HOST}${WS}"; then
  printf 'SB_WEB_RESULT=';jq -c --arg t "$tag" '.[$t]' "$ARGO_METADATA_FILE";return 0
 fi
 snapshot=$(mktemp -d "$DIR/install-backup.XXXXXXXX") || return 1
 cp "$CONFIG_FILE" "$snapshot/config" || return 1
 cp "$ARGO_METADATA_FILE" "$snapshot/meta" || return 1
 cp "$CLASH_YAML_FILE" "$snapshot/yaml" || return 1
 rollback_install() {
  local code=$?
  if [ "$committed" = 0 ]; then
   [ ! -s "$pidfile" ] || fixed_stop_pid "$(cat "$pidfile")" || true
   cp "$snapshot/config" "$CONFIG_FILE"
   cp "$snapshot/meta" "$ARGO_METADATA_FILE"
   cp "$snapshot/yaml" "$CLASH_YAML_FILE"
   _manage_service restart >/dev/null 2>&1 || true
  fi
  rm -rf "$snapshot"
  return "$code"
 }
 trap rollback_install RETURN
 _atomic_modify_json "$CONFIG_FILE" '.inbounds = ([.inbounds[] | select(.tag!=$t)] + [{type:"vless",tag:$t,listen:"127.0.0.1",listen_port:$p,users:[{uuid:$u,flow:""}],transport:{type:"ws",path:$w,max_early_data:2560,early_data_header_name:"Sec-WebSocket-Protocol"}}])' --arg t "$tag" --argjson p "$PORT" --arg u "$EXPECTED" --arg w "$WS" || return 1
 "$SINGBOX_BIN" check -c "$CONFIG_FILE" -c "$RELAY_CONFIG_FILE" || return 1
 _atomic_modify_json "$ARGO_METADATA_FILE" '.[$t]={name:$n,domain:$h,local_port:$p,uuid:$u,path:$w,protocol:"vless-ws",type:"fixed",manager:"argo-deploy-fixed",token:""}' --arg t "$tag" --arg n "$NAME" --arg h "$FIXED_HOST" --argjson p "$PORT" --arg u "$EXPECTED" --arg w "$WS" || return 1
 local proxy
 proxy=$(jq -n --arg n "$NAME" --arg h "$FIXED_HOST" --arg u "$EXPECTED" --arg w "$WS" '{name:$n,type:"vless",server:$h,port:443,uuid:$u,tls:true,udp:true,"skip-cert-verify":false,network:"ws",servername:$h,"ws-opts":{path:$w,"max-early-data":2560,"early-data-header-name":"Sec-WebSocket-Protocol",headers:{Host:$h}}}') || return 1
 _remove_node_from_yaml "$NAME" || return 1
 _add_node_to_yaml "$proxy" || return 1
 { cat "$BASE/library.sh"; printf '\nmain\n'; } > /usr/local/bin/sb.new || return 1
 chmod 700 /usr/local/bin/sb.new || return 1
 mv /usr/local/bin/sb.new /usr/local/bin/sb || return 1
 _manage_service restart || return 1
 oldpid=$(cat "$pidfile" 2>/dev/null || true)
 fixed_stop_pid "$oldpid" || return 1
 fixed_launch "$pidfile" "$logfile" || return 1
 fixed_wait "$WS" || return 1
 committed=1
 trap - RETURN
 rm -rf "$snapshot"
 printf 'SB_WEB_RESULT=';jq -c --arg t "$tag" '.[$t]' "$ARGO_METADATA_FILE"
}
if _with_state_lock install_fixed; then exit 0; else exit 1; fi
FIXED_INSTALL
"""
