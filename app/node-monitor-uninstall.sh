#!/bin/sh
set -eu
umask 077
[ "$(id -u)" = 0 ] || exit 1
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
CONF=/usr/local/etc/argo-monitor
if [ -f "$CONF/environment" ];then
 . "$CONF/environment"
 printf 'header = "Authorization: Bearer %s"\nsilent\nshow-error\nmax-time = 15\n' "$MONITOR_TOKEN" > "$TMP/curl.conf"
 curl --config "$TMP/curl.conf" -fsS -X POST "$MONITOR_SERVER/disable" >/dev/null || { echo '无法通知面板，暂未卸载；请恢复连接后重试';exit 1; }
fi
if command -v systemctl >/dev/null && [ -d /run/systemd/system ];then
 systemctl disable --now argo-monitor || true
 rm -f /etc/systemd/system/argo-monitor.service
 systemctl daemon-reload
elif command -v rc-service >/dev/null;then
 rc-service argo-monitor stop || true
 rc-update del argo-monitor default || true
 rm -f /etc/init.d/argo-monitor
fi
(crontab -l 2>/dev/null || true) | grep -v '# argo-monitor-publish$' > "$TMP/cron" || true
crontab "$TMP/cron"
rm -f /usr/local/lib/argo-deploy/monitor-agent /usr/local/lib/argo-deploy/monitor-run /usr/local/lib/argo-deploy/monitor-publish.sh
rm -rf "$CONF" /var/lib/argo-monitor
echo '资源探针已卸载，协议与远程管理组件保留'
