[ "$(id -u)" = 0 ] || { echo '请以 root 安装资源探针';exit 1; }
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
. /etc/os-release
case "$ID" in
 alpine) apk add --no-cache curl jq ca-certificates util-linux-misc dcron su-exec ;;
 debian|ubuntu) apt-get update -qq;DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl jq ca-certificates util-linux cron ;;
 *) echo '探针安装当前支持 Alpine / Debian / Ubuntu';exit 1;;
esac
case "$(uname -m)" in x86_64|amd64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; *) echo '探针支持 amd64 / arm64';exit 1;;esac
BASE=/usr/local/lib/argo-deploy
CONF=/usr/local/etc/argo-monitor
mkdir -p "$BASE" "$CONF" /var/lib/argo-monitor/services
chmod 755 "$BASE"
chmod 700 "$CONF";chmod 755 /var/lib/argo-monitor /var/lib/argo-monitor/services
if [ -s "$CONF/host-id" ] && [ "$(cat "$CONF/host-id")" != "$HOST_ID" ];then echo '此服务器已有另一主机探针，请在面板关联已有主机';exit 1;fi
TMP=$(mktemp -d /var/tmp/argo-monitor-install.XXXXXXXX)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
curl -fsS --max-time 30 "$PANEL/agent/releases/manifest.json" -o "$TMP/manifest"
NAME="monitor-agent-$ARCH"
SHA=$(jq -er --arg n "$NAME" '.files[$n].sha256 | select(test("^[a-f0-9]{64}$"))' "$TMP/manifest")
curl -fsS --retry 2 --max-time 180 "$PANEL/agent/releases/$NAME" -o "$TMP/probe"
printf '%s  %s\n' "$SHA" "$TMP/probe" | sha256sum -c -
chmod 755 "$TMP/probe"
for name in monitor-publish.sh monitor-uninstall.sh traffic.sh;do curl -fsS --max-time 60 "$PANEL/agent/assets/$name" -o "$TMP/$name";sh -n "$TMP/$name";done
if ! id argo-monitor >/dev/null 2>&1;then
 if [ "$ID" = alpine ];then addgroup -S argo-monitor;adduser -S -D -H -G argo-monitor argo-monitor;else useradd --system --no-create-home --shell /usr/sbin/nologin argo-monitor;fi
fi
cp "$TMP/probe" "$BASE/monitor-agent.new";chmod 755 "$BASE/monitor-agent.new";mv "$BASE/monitor-agent.new" "$BASE/monitor-agent"
for name in monitor-publish.sh monitor-uninstall.sh traffic.sh;do cp "$TMP/$name" "$BASE/$name";chmod 700 "$BASE/$name";done
printf '%s\n' "$HOST_ID" > "$CONF/host-id"
printf 'MONITOR_SERVER=%s/monitor\nMONITOR_TOKEN=%s\n' "$PANEL" "$MONITOR_TOKEN" > "$CONF/environment"
chmod 600 "$CONF/environment"
# Optional per-port accounting. Failed nft support never prevents protocol service.
if ! command -v nft >/dev/null 2>&1;then
 if [ "$ID" = alpine ];then apk add --no-cache nftables || true;else apt-get install -y --no-install-recommends nftables || true;fi
fi
touch /var/lib/argo-monitor/accounting
sh "$BASE/monitor-publish.sh"
(crontab -l 2>/dev/null || true) | grep -v '# argo-monitor-publish$' > "$TMP/cron" || true
printf '* * * * * sh %s/monitor-publish.sh >/dev/null 2>&1 # argo-monitor-publish\n' "$BASE" >> "$TMP/cron"
crontab "$TMP/cron"
if command -v systemctl >/dev/null && [ -d /run/systemd/system ];then
 cat > /etc/systemd/system/argo-monitor.service <<'UNIT'
[Unit]
Description=Argo Monitor resource probe
After=network-online.target
[Service]
User=argo-monitor
Group=argo-monitor
EnvironmentFile=/usr/local/etc/argo-monitor/environment
ExecStart=/usr/local/lib/argo-deploy/monitor-agent --interval 10
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
MemoryMax=64M
[Install]
WantedBy=multi-user.target
UNIT
 systemctl daemon-reload;systemctl enable --now cron;systemctl enable argo-monitor;systemctl restart argo-monitor
elif command -v rc-service >/dev/null;then
 cat > "$BASE/monitor-run" <<'RUN'
#!/bin/sh
set -a
. /usr/local/etc/argo-monitor/environment
set +a
exec su-exec argo-monitor /usr/local/lib/argo-deploy/monitor-agent --interval 10
RUN
 chmod 700 "$BASE/monitor-run"
 cat > /etc/init.d/argo-monitor <<'UNIT'
#!/sbin/openrc-run
name="Argo Monitor"
command="/usr/local/lib/argo-deploy/monitor-run"
supervisor="supervise-daemon"
respawn_delay=5
respawn_max=0
output_log="/dev/null"
error_log="/var/log/argo-monitor.log"
depend() { need net; }
UNIT
 chmod 755 /etc/init.d/argo-monitor
 rc-service dcron start 2>/dev/null || rc-service crond start
 rc-update add dcron default 2>/dev/null || rc-update add crond default
 rc-update add argo-monitor default;rc-service argo-monitor restart
else echo '缺少 systemd/OpenRC';exit 1;fi
# The probe must be observed before it can replace existing monitoring.
printf 'header = "Authorization: Bearer %s"\nsilent\nshow-error\nmax-time = 8\n' "$MONITOR_TOKEN" > "$TMP/curl.conf"
INSTALLED=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15;do
 if curl --config "$TMP/curl.conf" -fsS -X POST "$PANEL/monitor/activate" -o "$TMP/activated.json";then
  for id in $(jq -r '.nodes[]' "$TMP/activated.json");do
   dir="/usr/local/etc/argo-deploy/node-$id"
   [ -s "$dir/settings.json" ] || continue
   jq '.health_mode="monitor"' "$dir/settings.json" > "$dir/settings.json.new" && mv "$dir/settings.json.new" "$dir/settings.json"
  done
  echo 'Monitor 探针已接入，心跳来源切换成功';INSTALLED=1;break
 fi
 sleep 2
done
if [ "$INSTALLED" != 1 ];then
 echo '探针已安装，但尚未确认服务采样；旧监控仍保留，请检查后重试安装命令。'
 exit 1
fi
