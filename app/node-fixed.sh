#!/bin/bash
# Shared by migration and repair. Credentials never enter logs or process arguments.
fixed_credentials() {
 local panel cfg
 panel=$(jq -r '.panel' "$DIR/settings.json") || return 1
 cfg=$(mktemp "$DIR/fixed-config.XXXXXXXX") || return 1
 if ! curl --config "$DIR/curl.conf" -fsS "$panel/agent/fixed-config" -o "$cfg"; then rm -f "$cfg"; return 1; fi
 if ! jq -e '.hostname and (.tunnel_token | type=="string" and length>20)' "$cfg" >/dev/null; then rm -f "$cfg"; return 1; fi
 FIXED_HOST=$(jq -r '.hostname' "$cfg")
 jq -r '.tunnel_token' "$cfg" > "$DIR/tunnel-token.new" || { rm -f "$cfg"; return 1; }
 chmod 600 "$DIR/tunnel-token.new"
 mv "$DIR/tunnel-token.new" "$DIR/tunnel-token"
 rm -f "$cfg"
}
fixed_launch() {
 local pidfile=$1 logfile=$2
 if ! "$CLOUDFLARED_BIN" tunnel run --help 2>&1 | grep -q -- '--token-file'; then
  _install_cloudflared || return 1
 fi
 "$CLOUDFLARED_BIN" tunnel run --help 2>&1 | grep -q -- '--token-file' || { echo 'cloudflared 不支持 token-file，请更新后重试'; return 1; }
 _prepare_argo_runtime_dir || return 1
 nohup "$CLOUDFLARED_BIN" tunnel --protocol http2 --no-autoupdate run --token-file "$DIR/tunnel-token" > "$logfile" 2>&1 </dev/null 9>&- 7>&- 8>&- 6>&- &
 printf '%s\n' "$!" > "$pidfile"
 chmod 600 "$pidfile" "$logfile"
 sleep 2
 _is_pid_file_running_cmd "$pidfile" "$CLOUDFLARED_BIN"
}
fixed_ws() {
 local url=$1 headers key
 headers=$(mktemp "$DIR/ws.XXXXXXXX") || return 1
 key=$(head -c 16 /dev/urandom | base64 | tr -d '\n')
 curl --http1.1 --connect-timeout 3 --max-time 4 -sS -o /dev/null -D "$headers" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H "Sec-WebSocket-Key: $key" "$url" 2>/dev/null || true
 if grep -qE '^HTTP/1\.[01] 101([[:space:]]|$)' "$headers"; then rm -f "$headers"; return 0; fi
 rm -f "$headers"; return 1
}
fixed_wait() {
 local path=$1 i
 for i in {1..18}; do
  if fixed_ws "https://${FIXED_HOST}${path}"; then return 0; fi
  sleep 2
 done
 echo '固定域名 WebSocket 验证未通过，保留原节点'
 return 1
}
fixed_stop_pid() {
 local pid=$1 i
 if _is_pid_running_cmd "$pid" "$CLOUDFLARED_BIN"; then
  kill -TERM "$pid" 2>/dev/null || true
  # A process previously stopped with SIGSTOP must resume to handle TERM.
  kill -CONT "$pid" 2>/dev/null || true
  for i in {1..10}; do
   _is_pid_running_cmd "$pid" "$CLOUDFLARED_BIN" || return 0
   sleep 1
  done
  if _is_pid_running_cmd "$pid" "$CLOUDFLARED_BIN"; then kill -KILL "$pid"; fi
 fi
}
