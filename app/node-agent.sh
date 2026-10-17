#!/bin/sh
# A scheduled one-shot worker. It exits between ticks; credentials remain in root-only files.
set -u
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
DIR=${1:-}
case "$DIR" in /usr/local/etc/argo-deploy/node-[0-9]*) ;; *) exit 1;; esac
[ -f "$DIR/settings.json" ] && [ -f "$DIR/curl.conf" ] && [ ! -f "$DIR/disabled" ] || exit 0
exec 8>"$DIR/tick.lock"
flock -n 8 || exit 0
BASE=/usr/local/lib/argo-deploy
PANEL=$(jq -r '.panel' "$DIR/settings.json")
NODE_ID=$(jq -r '.node_id' "$DIR/settings.json")
WORK=$(mktemp -d /tmp/argo-tick.XXXXXXXX) || exit 1
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
HTTP_CODE=""
api() {
 method=$1;endpoint=$2;output=$3;input=${4:-}
 if [ -n "$input" ]; then
  HTTP_CODE=$(curl --http1.1 --config "$DIR/curl.conf" -X "$method" --data-binary @"$input" -o "$output" -w '%{http_code}' "$PANEL$endpoint") || return 1
 else
  HTTP_CODE=$(curl --http1.1 --config "$DIR/curl.conf" -X "$method" -o "$output" -w '%{http_code}' "$PANEL$endpoint") || return 1
 fi
 case "$HTTP_CODE" in 2??) return 0;; 401|410)
  # A deleted or re-bound record must stop polling, while leaving the actual node running.
  touch "$DIR/disabled"
  (crontab -l 2>/dev/null || true) | grep -vE "# argo-deploy-node-${NODE_ID}$" > "$WORK/cron" || true
  crontab "$WORK/cron"
  return 1;; *) return 1;; esac
}
load_meta() {
 PORT=$(jq -r '.port' "$DIR/settings.json")
 TAG="argo-vless-ws-${PORT}"
 jq -c --arg t "$TAG" '.[$t] | select(.protocol=="vless-ws" and .type=="temp") | {domain,uuid,path,local_port}' /usr/local/etc/sing-box/argo_metadata.json > "$WORK/meta.json" 2>/dev/null || true
 if [ ! -s "$WORK/meta.json" ]; then printf null > "$WORK/meta.json"; fi
}
update_setting() {
 field=$1;value=$2
 jq --arg k "$field" --argjson v "$value" '.[$k]=$v' "$DIR/settings.json" > "$DIR/settings.json.new" && mv "$DIR/settings.json.new" "$DIR/settings.json"
}
# If a callback was lost, send it again before taking another configuration task.
if [ -s "$DIR/pending-report.json" ]; then
 if api POST /agent/report "$WORK/ack.json" "$DIR/pending-report.json"; then
  REV=$(jq -r '.applied_revision' "$WORK/ack.json")
  update_setting applied_revision "$REV"
  rm -f "$DIR/pending-report.json"
 else exit 0; fi
fi
load_meta
PROCESS=false;LOCAL=false;PUBLIC=false;STATE=waiting;ERROR=""
FAILURES=$(cat "$DIR/failures" 2>/dev/null || echo 0)
LAST_REPAIR=$(cat "$DIR/last-repair" 2>/dev/null || echo 0)
REPAIRS=$(cat "$DIR/repairs" 2>/dev/null || echo 0)
case "$FAILURES:$LAST_REPAIR:$REPAIRS" in *[!0-9:]*|'') FAILURES=0;LAST_REPAIR=0;REPAIRS=0;; esac
ws_check() {
 url=$1;headers="$WORK/ws-headers"
 : > "$headers"
 ws_key=$(head -c 16 /dev/urandom | base64 | tr -d '\n')
 curl --http1.1 --connect-timeout 3 --max-time 4 -sS -o /dev/null -D "$headers" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H "Sec-WebSocket-Key: $ws_key" "$url" 2>/dev/null || true
 grep -qE '^HTTP/1\.[01] 101([[:space:]]|$)' "$headers"
}
check_health() {
 PROCESS=false;LOCAL=false;PUBLIC=false
 pid=$(cat "/run/singboxlite/argo-${PORT}.pid" 2>/dev/null || true)
 case "$pid" in ''|*[!0-9]*) ;; *)
  if kill -0 "$pid" 2>/dev/null && tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q cloudflared; then PROCESS=true; fi;; esac
 if jq -e 'type=="object"' "$WORK/meta.json" >/dev/null; then
  domain=$(jq -r '.domain' "$WORK/meta.json");path=$(jq -r '.path' "$WORK/meta.json")
  if ws_check "http://127.0.0.1:${PORT}${path}"; then LOCAL=true; fi
  if ws_check "https://${domain}${path}"; then PUBLIC=true; fi
 fi
}
send_state() {
 state=$1;error=$2;out=$3
 jq -n --arg state "$state" --arg error "$error" --argjson process "$PROCESS" --argjson local "$LOCAL" --argjson public "$PUBLIC" \
 --argjson failures "$FAILURES" --argjson last "$LAST_REPAIR" --argjson repairs "$REPAIRS" --slurpfile meta "$WORK/meta.json" \
 '{state:$state,error:$error,metadata:$meta[0],process_alive:$process,local_ws:$local,public_ws:$public,failure_count:$failures,last_repair:$last,repair_count:$repairs}' > "$out"
}
run_repair() {
 mode=$1;limit=$2
 timeout "$limit" bash "$BASE/repair.sh" "$DIR" "$mode" >> "$WORK/repair.log" 2>&1 8>&- 6>&- &
 repair_pid=$!;progress_at=$(date +%s)
 while kill -0 "$repair_pid" 2>/dev/null; do
  sleep 2
  current=$(date +%s)
  if [ $((current-progress_at)) -ge 30 ]; then
   send_state repairing '正在检查并重建隧道' "$WORK/repair-progress.json"
   api POST /agent/report "$WORK/ack.json" "$WORK/repair-progress.json" || true
   progress_at=$current
  fi
 done
 wait "$repair_pid"
}
if jq -e 'type=="object"' "$WORK/meta.json" >/dev/null; then
 check_health
 if [ "$LOCAL" = true ] && [ "$PUBLIC" = true ] && [ "$PROCESS" = true ]; then FAILURES=0;STATE=online
 else
  FAILURES=$((FAILURES+1));STATE=degraded;ERROR='本地服务或公网 WebSocket 隧道检查未通过'
  NOW=$(date +%s)
  if [ "$FAILURES" -ge 3 ] && [ $((NOW-LAST_REPAIR)) -ge 300 ]; then
   # A general egress outage must not create a tunnel restart loop.
   if curl --connect-timeout 3 --max-time 5 -fsS https://www.cloudflare.com/cdn-cgi/trace >/dev/null 2>&1; then
    LAST_REPAIR=$NOW;REPAIRS=$((REPAIRS+1));printf '%s\n' "$LAST_REPAIR" > "$DIR/last-repair";printf '%s\n' "$REPAIRS" > "$DIR/repairs"
    if [ "$LOCAL" != true ]; then
     run_repair service 60 || true
     check_health
    fi
    if [ "$PUBLIC" != true ] || [ "$PROCESS" != true ]; then
     run_repair tunnel 210 || true
     load_meta;check_health
    fi
    if [ "$LOCAL" = true ] && [ "$PUBLIC" = true ] && [ "$PROCESS" = true ]; then FAILURES=0;STATE=online;ERROR='';else STATE=degraded;ERROR='自动修复后隧道仍未通过检查，已进入 5 分钟冷却';fi
   else STATE=network_error;ERROR='服务器出站网络检查失败，暂不重建隧道';fi
  fi
 fi
fi
printf '%s\n' "$FAILURES" > "$DIR/failures"
send_state "$STATE" "$ERROR" "$WORK/report.json"
api POST /agent/report "$WORK/ack.json" "$WORK/report.json" || exit 0
[ ! -f "$DIR/disabled" ] || exit 0
api GET /agent/work "$WORK/work.json" || exit 0
if ! jq -e '.job!=null' "$WORK/work.json" >/dev/null; then exit 0; fi
REV=$(jq -r '.job.revision' "$WORK/work.json")
NEW_PORT=$(jq -r '.job.config_json | fromjson | .node_port' "$WORK/work.json")
# Serialize actual sing-box modifications across agent-managed nodes on one server.
exec 6>/run/argo-deploy-job.lock
flock -n 6 || exit 0
api GET "/agent/job/${REV}.sh" "$WORK/job.sh" || exit 0
chmod 700 "$WORK/job.sh"
echo "正在执行配置版本 $REV…"
timeout 1500 sh "$WORK/job.sh" > "$WORK/job.log" 2>&1 8>&- 6>&- &
JOB_PID=$!
LAST_PROGRESS=$(date +%s)
while kill -0 "$JOB_PID" 2>/dev/null; do
 sleep 2
 NOW=$(date +%s)
 if [ $((NOW-LAST_PROGRESS)) -ge 30 ]; then
  send_state installing '' "$WORK/progress.json"
  api POST /agent/report "$WORK/ack.json" "$WORK/progress.json" || true
  LAST_PROGRESS=$NOW
 fi
done
if wait "$JOB_PID"; then CODE=0;else CODE=$?;fi
# Successful uninstall removes its own profile and confirms directly to the panel.
if [ "$CODE" = 0 ] && [ ! -f "$DIR/settings.json" ]; then cat "$WORK/job.log"; exit 0; fi
sed -n 's/^SB_WEB_RESULT=//p' "$WORK/job.log" | tail -1 > "$WORK/result.json"
if [ "$CODE" = 0 ] && jq -e '.domain and .uuid and .path and .local_port' "$WORK/result.json" >/dev/null 2>&1; then
 update_setting port "$NEW_PORT"
 load_meta;check_health
 if [ "$LOCAL" = true ] && [ "$PUBLIC" = true ]; then STATE=online;ERROR='';else STATE=degraded;ERROR='配置已应用，连通性检查尚未通过';fi
 send_state "$STATE" "$ERROR" "$WORK/base-report.json"
 jq --argjson rev "$REV" '. + {job_revision:$rev,job_ok:true}' "$WORK/base-report.json" > "$DIR/pending-report.json"
else
 load_meta
 # A failed migration leaves the old listener and link intact.
 ERROR="配置执行失败（退出码 $CODE），请查看任务日志"
 send_state error "$ERROR" "$WORK/base-report.json"
 jq --argjson rev "$REV" '. + {job_revision:$rev,job_ok:false}' "$WORK/base-report.json" > "$DIR/pending-report.json"
fi
# Redact shell escape sequences; retain only a bounded tail of diagnostics.
tail -35 "$WORK/job.log" | sed '/^SB_WEB_RESULT=/d' | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' > "$WORK/log-tail"
jq --rawfile log "$WORK/log-tail" '.logs=($log|split("\n")|map(select(length>0)))' "$DIR/pending-report.json" > "$DIR/pending-report.new"
mv "$DIR/pending-report.new" "$DIR/pending-report.json"
if api POST /agent/report "$WORK/ack.json" "$DIR/pending-report.json"; then
 update_setting applied_revision "$(jq -r '.applied_revision' "$WORK/ack.json")"
 rm -f "$DIR/pending-report.json"
fi
cat "$WORK/log-tail"
