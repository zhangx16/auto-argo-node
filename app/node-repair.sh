#!/bin/bash
# Only invoked on three consecutive health failures, never on each healthy heartbeat.
set -e
DIR="$1"
MODE="$2"
PORT=$(jq -r '.port' "$DIR/settings.json")
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || exit 1
if source /usr/local/lib/argo-deploy/library.sh agent-library; then :; else exit 1; fi
SELF_SCRIPT_PATH=/usr/local/bin/sb
SCRIPT_DIR=/usr/local/bin
_detect_init_system
run_step() { if "$@"; then return 0; else return 1; fi; }
if [ "$MODE" = service ]; then run_step _manage_service restart; exit $?; fi
exec 7>"$RUNTIME_DIR/argo-keepalive.lock"
flock -n 7 || exit 0
repair_locked() {
 local tag="argo-vless-ws-${PORT}" domain old_pid
 if jq -e --arg t "$tag" '.[$t] | select(.type=="fixed" and .manager=="argo-deploy-fixed")' "$ARGO_METADATA_FILE" >/dev/null; then
  source /usr/local/lib/argo-deploy/fixed.sh
  fixed_credentials || return 1
  [ "$FIXED_HOST" = "$(jq -r --arg t "$tag" '.[$t].domain' "$ARGO_METADATA_FILE")" ] || return 1
  old_pid=$(cat "$(_argo_pid_file "$PORT")" 2>/dev/null || true)
  fixed_stop_pid "$old_pid" || return 1
  fixed_launch "$(_argo_pid_file "$PORT")" "$(_argo_log_file "$PORT")" || return 1
  fixed_wait "$(jq -r --arg t "$tag" '.[$t].path' "$ARGO_METADATA_FILE")"
  return $?
 fi
 jq -e --arg t "$tag" '.[$t] | select(.type=="temp" and .protocol=="vless-ws")' "$ARGO_METADATA_FILE" >/dev/null || return 1
 old_pid=$(cat "$(_argo_pid_file "$PORT")" 2>/dev/null || true)
 _stop_argo_tunnel "$PORT" || return 1
 sleep 1
 if _is_pid_running_cmd "$old_pid" "$CLOUDFLARED_BIN"; then kill -KILL "$old_pid" || return 1; fi
 domain=$(_start_argo_tunnel "$PORT" vless-ws 9>&- 7>&- 8>&- 6>&-) || return 1
 [[ "$domain" =~ ^[a-z0-9-]+\.trycloudflare\.com$ ]] || return 1
 _atomic_modify_json "$ARGO_METADATA_FILE" '.[$t].domain=$d' --arg t "$tag" --arg d "$domain" || return 1
 export NODE_DOMAIN="$domain" NODE_NAME
 NODE_NAME=$(jq -r --arg t "$tag" '.[$t].name' "$ARGO_METADATA_FILE") || return 1
 _atomic_modify_yaml "$CLASH_YAML_FILE" '(.proxies[] | select(.name==env(NODE_NAME))) |= (.server=env(NODE_DOMAIN) | .servername=env(NODE_DOMAIN) | .["ws-opts"].headers.Host=env(NODE_DOMAIN))' || return 1
}
if _with_state_lock repair_locked; then result=0; else result=1; fi
flock -u 7
exit "$result"
