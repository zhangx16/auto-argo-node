import shlex
import json


def bootstrap_script(panel, enrollment_token, node_id, options=None):
    constants='\n'.join(k+'='+shlex.quote(str(v)) for k,v in {'PANEL':panel.rstrip('/'),'ENROLLMENT':enrollment_token,'NODE_ID':node_id}.items())
    script = '''#!/bin/sh
set -eu
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
''' + constants + '''
[ "$(id -u)" = 0 ] || { echo '请以 root 执行安装命令'; exit 1; }
DIR="/usr/local/etc/argo-deploy/node-${NODE_ID}"
LOCKED=0
if [ -f "$DIR/settings.json" ] && command -v flock >/dev/null; then
 exec 8>"$DIR/tick.lock"
 echo '等待该节点当前任务结束后更新接入配置…'
 flock 8
 LOCKED=1
fi
. /etc/os-release
case "$ID" in
 alpine) apk add --no-cache bash curl jq ca-certificates util-linux-misc dcron ;;
 debian|ubuntu) apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y bash curl jq ca-certificates util-linux cron ;;
 *) echo '当前仅支持 Alpine / Debian / Ubuntu'; exit 1 ;;
esac
# Optional kernel traffic dependency. Unsupported NAT kernels remain usable.
if ! command -v nft >/dev/null 2>&1;then
 case "$ID" in alpine) apk add --no-cache nftables || true;; *) apt-get install -y --no-install-recommends nftables || true;; esac
fi
BASE=/usr/local/lib/argo-deploy
DIR="/usr/local/etc/argo-deploy/node-${NODE_ID}"
mkdir -p "$BASE" "$DIR"
chmod 700 "$DIR"
if [ "$LOCKED" = 0 ]; then exec 8>"$DIR/tick.lock"; flock 8; fi
TMP=$(mktemp -d /tmp/argo-enroll.XXXXXXXX)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
for name in agent.sh repair.sh library.sh fixed.sh traffic.sh resources.sh diagnose.sh core.sh; do
 curl --http1.1 --connect-timeout 10 --max-time 90 -fsSL "$PANEL/agent/assets/$name" -o "$TMP/$name"
 chmod 700 "$TMP/$name"
done
# Exchange the one-time enrollment credential for a scoped node credential.
IP=$(curl -4 -fsS --max-time 4 https://api.ipify.org 2>/dev/null || true)
jq -n --arg token "$ENROLLMENT" --arg hostname "$(hostname)" --arg os "$ID" --arg arch "$(uname -m)" --arg ip "$IP" \
 '{enrollment_token:$token,hostname:$hostname,os:$os,arch:$arch,ip:$ip}' > "$TMP/enroll.json"
curl --http1.1 --connect-timeout 10 --max-time 25 -fsS -H 'Content-Type: application/json' --data-binary @"$TMP/enroll.json" "$PANEL/agent/enroll" -o "$TMP/reply.json"
jq -e --argjson id "$NODE_ID" '.node_id == $id and (.agent_token | type == "string")' "$TMP/reply.json" >/dev/null
jq --arg panel "$PANEL" '{panel:$panel,node_id:.node_id,port:.port,applied_revision:.applied_revision,protocol:(.protocol // "vless-ws-argo"),core_channel:(.core_channel // "legacy"),management_enabled:(.management_enabled // false),health_mode:(.health_mode // "legacy")}' "$TMP/reply.json" > "$DIR/settings.json.new"
jq -r '.agent_token | "header = \\"X-Agent-Token: " + . + "\\""' "$TMP/reply.json" > "$DIR/curl.conf.new"
printf '%s\\n' 'header = "Content-Type: application/json"' 'silent' 'show-error' 'connect-timeout = 8' 'max-time = 30' >> "$DIR/curl.conf.new"
chmod 600 "$DIR/settings.json.new" "$DIR/curl.conf.new"
mv "$DIR/settings.json.new" "$DIR/settings.json"
mv "$DIR/curl.conf.new" "$DIR/curl.conf"
for name in agent.sh repair.sh library.sh fixed.sh traffic.sh resources.sh diagnose.sh core.sh; do mv "$TMP/$name" "$BASE/$name"; done
rm -f "$DIR/disabled"
# Reuse the system's cron daemon; no Python or persistent monitoring process.
if ! command -v crontab >/dev/null 2>&1; then
 if command -v apk >/dev/null; then apk add --no-cache dcron; else apt-get install -y cron; fi
fi
TAG="# argo-deploy-node-${NODE_ID}"
(crontab -l 2>/dev/null || true) | grep -vE "${TAG}$" > "$TMP/cron" || true
printf '* * * * * sh %s/agent.sh %s >/dev/null 2>&1 %s\\n' "$BASE" "$DIR" "$TAG" >> "$TMP/cron"
crontab "$TMP/cron"
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
 systemctl enable --now cron
elif command -v rc-service >/dev/null; then
 rc-service dcron start 2>/dev/null || rc-service crond start
 rc-update add dcron default 2>/dev/null || rc-update add crond default
else echo '缺少 systemd/OpenRC，无法保证重启后监控运行'; exit 1
fi
flock -u 8
exec 8>&-
echo '节点已绑定，正在执行首次部署或读取已有配置…'
sh "$BASE/agent.sh" "$DIR"
echo '安装命令执行完毕。请在面板查看结果；后续每分钟检查和上报一次。'
'''

    if options and options.get('ssh_transport'):
        script=script.replace('sh "$BASE/agent.sh" "$DIR"', '''bash -c 'curl() { command curl -H "X-SSH-Execution: 1" "$@"; }; export -f curl; exec bash "$1/agent.sh" "$2"' argo-ssh-bootstrap "$BASE" "$DIR"''')
    return script
