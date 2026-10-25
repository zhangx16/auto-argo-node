#!/bin/sh
# Restore the existing per-node cron worker before removing the resource probe.
set -eu
umask 077
BASE=/usr/local/lib/argo-deploy
TMP=$(mktemp -d /tmp/argo-heartbeat-restore.XXXXXXXX)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
case "${1:-}" in
 restore)
  (crontab -l 2>/dev/null || true) > "$TMP/cron"
  for profile in /usr/local/etc/argo-deploy/node-*;do
   [ -s "$profile/settings.json" ] && [ -s "$profile/curl.conf" ] && [ ! -f "$profile/disabled" ] || continue
   id=$(jq -er '.node_id' "$profile/settings.json")
   case "$id" in ''|*[!0-9]*) exit 1;;esac
   jq '.health_mode="legacy" | .management_enabled=true' "$profile/settings.json" > "$profile/settings.json.new"
   chmod 600 "$profile/settings.json.new";mv "$profile/settings.json.new" "$profile/settings.json"
   grep -vE "# argo-deploy-node-${id}$" "$TMP/cron" > "$TMP/next" || true
   mv "$TMP/next" "$TMP/cron"
   printf '* * * * * sh %s/agent.sh %s >/dev/null 2>&1 # argo-deploy-node-%s\n' "$BASE" "$profile" "$id" >> "$TMP/cron"
  done
  crontab "$TMP/cron"
  if [ -d /run/systemd/system ];then systemctl enable --now cron;else rc-service dcron start 2>/dev/null || rc-service crond start;fi
  for profile in /usr/local/etc/argo-deploy/node-*;do
   [ -s "$profile/settings.json" ] && [ -s "$profile/curl.conf" ] && [ ! -f "$profile/disabled" ] || continue
   panel=$(jq -er '.panel' "$profile/settings.json")
   curl --config "$profile/curl.conf" -fsS -X POST "$panel/agent/restore-heartbeat" >/dev/null
  done
  echo '节点 cron、心跳和管理已恢复，资源探针暂保留，等待面板验证真实上报。'
  ;;
 remove)
  for profile in /usr/local/etc/argo-deploy/node-*;do
   [ -s "$profile/settings.json" ] && [ -s "$profile/curl.conf" ] && [ ! -f "$profile/disabled" ] || continue
   panel=$(jq -er '.panel' "$profile/settings.json")
   curl --config "$profile/curl.conf" -fsS "$panel/agent/work" -o "$TMP/control"
   jq -e '.health_mode=="legacy"' "$TMP/control" >/dev/null || { echo '仍有节点依赖探针，拒绝卸载';exit 1; }
  done
  if [ -d /run/systemd/system ];then
   systemctl disable --now argo-monitor 2>/dev/null || true
   rm -f /etc/systemd/system/argo-monitor.service
   systemctl daemon-reload
  elif command -v rc-service >/dev/null;then
   rc-service argo-monitor stop 2>/dev/null || true
   rc-update del argo-monitor default 2>/dev/null || true
   rm -f /etc/init.d/argo-monitor
  fi
  (crontab -l 2>/dev/null || true) | grep -v '# argo-monitor-publish$' > "$TMP/cron" || true
  crontab "$TMP/cron"
  rm -f "$BASE/monitor-agent" "$BASE/monitor-run" "$BASE/monitor-publish.sh" "$BASE/monitor-uninstall.sh" /var/log/argo-monitor.log
  rm -rf /usr/local/etc/argo-monitor /var/lib/argo-monitor
  if ps -eo comm 2>/dev/null | grep -qx monitor-agent;then echo '探针进程仍存在，请检查';exit 1;fi
  echo '独立 Monitor 探针服务、二进制、配置与定时发布任务已删除；节点协议和 cron 管理保留。'
  ;;
 *) exit 1;;
esac
