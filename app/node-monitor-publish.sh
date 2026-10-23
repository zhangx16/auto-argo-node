#!/bin/sh
# One host-wide privileged snapshot. No networking; the probe reads only this output.
set -u
umask 077
BASE=/usr/local/lib/argo-deploy
OUT=/var/lib/argo-monitor/services
[ -d "$OUT" ] || exit 0
exec 7>/run/argo-monitor-publish.lock
flock -n 7 || exit 0
WORK=$(mktemp -d /tmp/argo-publish.XXXXXXXX) || exit 1
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
if [ -f /var/log/argo-monitor.log ] && [ "$(wc -c < /var/log/argo-monitor.log)" -gt 65536 ];then
 tail -50 /var/log/argo-monitor.log > "$WORK/probe.log" && cat "$WORK/probe.log" > /var/log/argo-monitor.log
fi
for DIR in /usr/local/etc/argo-deploy/node-*;do
 [ -s "$DIR/settings.json" ] && [ ! -f "$DIR/disabled" ] || continue
 NODE_ID=$(jq -er '.node_id' "$DIR/settings.json") || continue
 case "$NODE_ID" in ''|*[!0-9]*) continue;;esac
 PORT=$(jq -r '.port' "$DIR/settings.json");PROTOCOL=$(jq -r '.protocol // "vless-ws-argo"' "$DIR/settings.json")
 printf null > "$WORK/traffic.json"
 if [ -f /var/lib/argo-monitor/accounting ] && [ -f "$BASE/traffic.sh" ];then . "$BASE/traffic.sh";traffic_sample;fi
 printf '{}' > "$WORK/meta.json"
 if [ "$PROTOCOL" = vless-ws-argo ];then
  jq -c --arg t "argo-vless-ws-${PORT}" '.[$t] | {domain:(.domain // ""),path:(.path // "")}' /usr/local/etc/sing-box/argo_metadata.json > "$WORK/meta.json" 2>/dev/null || printf '{}' > "$WORK/meta.json"
 fi
 [ -s /usr/local/etc/sing-box/core-install.json ] && cp /usr/local/etc/sing-box/core-install.json "$WORK/core.json" || printf null > "$WORK/core.json"
 jq --argjson at "$(date +%s)" --slurpfile meta "$WORK/meta.json" --slurpfile traffic "$WORK/traffic.json" --slurpfile core "$WORK/core.json" '{node_id,port,protocol:(.protocol // "vless-ws-argo"),revision:.applied_revision,published_at:$at,domain:($meta[0].domain // ""),path:($meta[0].path // ""),traffic:$traffic[0],core:$core[0]}' "$DIR/settings.json" > "$OUT/$NODE_ID.json.new" || continue
 chmod 644 "$OUT/$NODE_ID.json.new";mv "$OUT/$NODE_ID.json.new" "$OUT/$NODE_ID.json"
done
for f in "$OUT"/*.json;do
 [ -f "$f" ] || continue
 id=${f##*/};id=${id%.json}
 [ -f "/usr/local/etc/argo-deploy/node-$id/settings.json" ] && [ ! -f "/usr/local/etc/argo-deploy/node-$id/disabled" ] || rm -f "$f"
done
