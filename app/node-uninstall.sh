#!/bin/sh
set -eu
DIR=/usr/local/etc/argo-deploy/node-__NODE_ID__
EXPECTED=__UUID__
PORT=$(jq -r '.port' "$DIR/settings.json")
BASE=/usr/local/lib/argo-deploy
# Download a fresh library for older enrolled agents as well.
PANEL=$(jq -r '.panel' "$DIR/settings.json")
curl --config "$DIR/curl.conf" -fsS "$PANEL/agent/assets/library.sh" -o "$DIR/uninstall-library.sh"
export DIR EXPECTED PORT
bash <<'BASH'
set -e
if source "$DIR/uninstall-library.sh" agent-library; then :; else exit 1; fi
SELF_SCRIPT_PATH=/usr/local/bin/sb
_detect_init_system
remove_owned() {
 local tag="argo-vless-ws-${PORT}" name actual
 if [ -f "$ARGO_METADATA_FILE" ]; then
  actual=$(jq -r --arg t "$tag" '.[$t].uuid // empty' "$ARGO_METADATA_FILE") || return 1
  [ -z "$actual" ] || [ "$actual" = "$EXPECTED" ] || { echo 'UUID 不匹配，拒绝卸载'; return 1; }
  name=$(jq -r --arg t "$tag" '.[$t].name // empty' "$ARGO_METADATA_FILE") || return 1
 fi
 if [ -f "$CONFIG_FILE" ]; then
  jq -e --arg t "$tag" --arg u "$EXPECTED" '[.inbounds[]? | select(.tag==$t) | .users[]?.uuid] | all(.==$u)' "$CONFIG_FILE" >/dev/null || return 1
  _atomic_modify_json "$CONFIG_FILE" 'del(.inbounds[] | select(.tag==$t))' --arg t "$tag" || return 1
 fi
 _stop_argo_tunnel "$PORT" || return 1
 if [ -f "$ARGO_METADATA_FILE" ]; then _atomic_modify_json "$ARGO_METADATA_FILE" 'del(.[$t])' --arg t "$tag" || return 1; fi
 if [ -n "${name:-}" ] && [ -f "$CLASH_YAML_FILE" ]; then _remove_node_from_yaml "$name" || return 1; fi
 rm -f "/var/log/singbox_argo_${PORT}.log" "/run/singboxlite/argo-${PORT}.pid"
 if [ -f "$ARGO_METADATA_FILE" ] && [ "$(jq length "$ARGO_METADATA_FILE")" = 0 ]; then _disable_argo_watchdog || return 1; fi
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
if _with_state_lock remove_owned; then :; else exit 1; fi
BASH
# Remove only this node's dedicated optional traffic counters.
if command -v nft >/dev/null;then nft delete table inet argo_traffic___NODE_ID__ 2>/dev/null || true;fi
# Keep credentials and the cron retry until the panel acknowledges successful cleanup.
curl --config "$DIR/curl.conf" --retry 3 --retry-all-errors -fsS -X POST "$PANEL/agent/uninstalled/__REVISION__" >/dev/null
(crontab -l 2>/dev/null || true) | grep -vE '# argo-deploy-node-__NODE_ID__$' > "$DIR/cron.new" || true
crontab "$DIR/cron.new"
rm -rf "$DIR"
if ! find /usr/local/etc/argo-deploy -name settings.json -print | grep -q .; then
 rm -rf /usr/local/lib/argo-deploy /usr/local/etc/argo-deploy
fi
exit 0
