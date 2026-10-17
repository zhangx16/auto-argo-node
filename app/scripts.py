INSTALL_SH = r'''#!/bin/bash
# singbox-mgmt bootstrap — VLESS Reality via singbox-lite
# token: __TOKEN__
set -u
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
umask 077

PANEL='__PANEL__'
PANEL_HOST='__PANEL_HOST__'
ORIGIN_IP='__ORIGIN_IP__'
TOKEN='__TOKEN__'
NODE_PORT='__NODE_PORT__'
REMARK='__REMARK__'
SNI='__SNI__'
CLIENT_ADDRESS='__CLIENT_ADDRESS__'
SB_URL='https://raw.githubusercontent.com/0xdabiaoge/singbox-lite/main/singbox.sh'
META=/usr/local/etc/sing-box/metadata.json
CONF=/usr/local/etc/sing-box/config.json
AGENT_ENV=/usr/local/etc/singbox-mgmt-agent.env
TAG="vless-in-${NODE_PORT}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行。"
  exit 1
fi

strip() { printf '%s' "$1" | tr -d '\r'; }

# 节点机直连源站，避开 Cloudflare 橙云对 curl TLS 握手的重置。
curl_panel() {
  if [ -n "$ORIGIN_IP" ]; then
    curl -4 --http1.1 --tlsv1.2 --resolve "${PANEL_HOST}:443:${ORIGIN_IP}" "$@"
  else
    curl -4 --http1.1 --tlsv1.2 "$@"
  fi
}

report() {
  local msg
  msg=$(strip "$1")
  echo "$msg"
  curl_panel -fsS -m 15 -X POST "$PANEL/a/log" \
    -H "X-Node-Token: $TOKEN" \
    -H "Content-Type: text/plain; charset=utf-8" \
    --data-binary "$msg" >/dev/null 2>&1 || true
}

callback() {
  local path="$1"
  shift
  curl_panel -fsS -m 20 -X POST "$PANEL$path" \
    -H "X-Node-Token: $TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "$1" >/dev/null 2>&1 || true
}

fail() {
  report "错误: $1"
  callback /a/result "{\"ok\":false,\"error\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1" 2>/dev/null || echo "\"$1\"")}"
  exit 1
}

detect_ip() {
  local ip=""
  ip=$(curl -4 -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)
  [ -n "$ip" ] || ip=$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)
  [ -n "$ip" ] || ip=$(curl -4 -fsS --max-time 8 https://ipv4.icanhazip.com 2>/dev/null || true)
  [ -n "$ip" ] || ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  printf '%s' "$ip" | tr -d ' \r\n'
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1" 2>/dev/null || {
    printf '"'
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
    printf '"'
  }
}

install_agent() {
  cat > "$AGENT_ENV" <<EOF
PANEL='$PANEL'
PANEL_HOST='$PANEL_HOST'
ORIGIN_IP='$ORIGIN_IP'
TOKEN='$TOKEN'
NODE_PORT='$NODE_PORT'
EOF
  chmod 600 "$AGENT_ENV"
  if command -v crontab >/dev/null 2>&1; then
    local cron_line="*/5 * * * * . $AGENT_ENV; curl -4 --http1.1 --tlsv1.2 --resolve \"\${PANEL_HOST}:443:\${ORIGIN_IP}\" -fsS -m 10 -X POST \"\$PANEL/a/heartbeat\" -H \"X-Node-Token: \$TOKEN\" >/dev/null 2>&1"
    (crontab -l 2>/dev/null | grep -v singbox-mgmt-agent.env; echo "$cron_line") | crontab - 2>/dev/null || true
    report "已写入 5 分钟心跳"
  else
    report "无 crontab，跳过心跳（不影响节点使用）"
  fi
}

install_pkg() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y >/dev/null 2>&1 || true
    apt-get install -y "$@" >/dev/null 2>&1 || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache "$@" >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y "$@" >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y "$@" >/dev/null 2>&1 || true
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 && return 0
  case "$1" in
    curl) install_pkg curl ca-certificates ;;
    jq) install_pkg jq ;;
    python3) install_pkg python3 ;;
  esac
  command -v "$1" >/dev/null 2>&1
}

need_cmd curl || fail "无法安装 curl"
need_cmd jq || report "jq 暂未安装，将在 sb 初始化后再试"
need_cmd python3 || true

HOST_IP=$(detect_ip)
HOSTNAME_S=$(hostname 2>/dev/null || echo unknown)
OS_INFO=$(grep -E '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d'"' -f2)
[ -n "$OS_INFO" ] || OS_INFO=$(uname -srm)
LINK_IP="$CLIENT_ADDRESS"
[ -n "$LINK_IP" ] || LINK_IP="$HOST_IP"

callback /a/hello "$(cat <<JSON
{"hostname":$(json_escape "$HOSTNAME_S"),"ip":$(json_escape "$HOST_IP"),"os":$(json_escape "$OS_INFO"),"node_port":${NODE_PORT}}
JSON
)"

report "已连接面板，开始安装 singbox-lite"
report "主机 ${HOSTNAME_S}  公网IP ${HOST_IP}  节点端口 ${NODE_PORT}  备注 ${REMARK}"

if [ -s "$META" ] && command -v jq >/dev/null 2>&1; then
  EXISTING=$(jq -r --arg t "$TAG" '.[$t].share_link // empty' "$META" 2>/dev/null || true)
  if [ -n "$EXISTING" ]; then
    report "检测到端口 ${NODE_PORT} 已有 Reality 节点，跳过创建并回传链接"
    callback /a/result "$(cat <<JSON
{"ok":true,"share_link":$(json_escape "$EXISTING"),"ip":$(json_escape "$HOST_IP"),"hostname":$(json_escape "$HOSTNAME_S"),"os":$(json_escape "$OS_INFO"),"tag":$(json_escape "$TAG")}
JSON
)"
    install_agent
    exit 0
  fi
fi

if command -v ss >/dev/null 2>&1; then
  if ss -tlnp 2>/dev/null | grep -qE ":${NODE_PORT}\\b"; then
    if [ ! -s "$META" ] || ! jq -e --arg t "$TAG" '.[$t]' "$META" >/dev/null 2>&1; then
      fail "端口 ${NODE_PORT} 已被其他进程占用"
    fi
  fi
fi

report "下载 singbox-lite 主脚本"
if ! curl -LfsS --retry 3 --retry-delay 2 "$SB_URL" -o /usr/local/bin/sb; then
  wget -q "$SB_URL" -O /usr/local/bin/sb || fail "下载 singbox.sh 失败（需要能访问 GitHub）"
fi
chmod +x /usr/local/bin/sb
head -1 /usr/local/bin/sb | grep -q bash || fail "下载的 sb 脚本无效"

run_sb() {
  local answers="$1"
  local seconds="${2:-900}"
  local log=/tmp/singbox-mgmt-sb.log
  : > "$log"
  report "调用 sb（超时 ${seconds}s）"
  if command -v timeout >/dev/null 2>&1; then
    printf '%s' "$answers" | timeout "$seconds" /usr/local/bin/sb >"$log" 2>&1 || true
  else
    printf '%s' "$answers" | /usr/local/bin/sb >"$log" 2>&1 || true
  fi
  tail -n 40 "$log" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | while IFS= read -r line; do
    [ -n "$line" ] && report "sb: $line"
  done
}

report "初始化依赖（首次会比较久）"
run_sb '0\n' 600

if [ ! -x /usr/local/bin/sing-box ]; then
  report "安装 sing-box 核心（菜单 15 -> 1 最新稳定版）"
  run_sb '15\n1\n.0\n' 900
fi

[ -x /usr/local/bin/sing-box ] || fail "sing-box 核心安装失败"

need_cmd jq || fail "jq 安装失败，无法读取节点元数据"

report "创建 VLESS + TCP + Reality + Vision 节点"
# 主菜单 1 -> 添加协议 1 -> IP / SNI / 端口 / 名称 -> 任意键 -> 退出
# 任意键使用 '.'，避免吞掉后续菜单数字
if [ -n "$CLIENT_ADDRESS" ]; then
  run_sb "1\n1\n${CLIENT_ADDRESS}\n${SNI}\n${NODE_PORT}\n${REMARK}\n.0\n" 300
else
  run_sb "1\n1\n\n${SNI}\n${NODE_PORT}\n${REMARK}\n.0\n" 300
fi

harvest_link() {
  local url=""
  if [ -s "$META" ]; then
    url=$(jq -r --arg t "$TAG" '.[$t].share_link // empty' "$META" 2>/dev/null || true)
  fi
  if [ -z "$url" ] && [ -s "$CONF" ] && [ -s "$META" ]; then
    local uuid pk sid sni_v
    uuid=$(jq -r --argjson p "$NODE_PORT" '.inbounds[]? | select(.listen_port == $p and .type == "vless") | .users[0].uuid' "$CONF" 2>/dev/null | head -1)
    pk=$(jq -r --arg t "$TAG" '.[$t].publicKey // empty' "$META" 2>/dev/null)
    sid=$(jq -r --arg t "$TAG" '.[$t].shortId // empty' "$META" 2>/dev/null)
    sni_v=$(jq -r --argjson p "$NODE_PORT" '.inbounds[]? | select(.listen_port == $p) | .tls.server_name // .tls.reality.handshake.server // empty' "$CONF" 2>/dev/null | head -1)
    [ -n "$sni_v" ] || sni_v="$SNI"
    if [ -n "$uuid" ] && [ -n "$pk" ] && [ -n "$sid" ] && [ -n "$LINK_IP" ]; then
      local host="$LINK_IP"
      case "$host" in
        *:*) host="[$host]" ;;
      esac
      url="vless://${uuid}@${host}:${NODE_PORT}?security=reality&encryption=none&pbk=${pk}&fp=chrome&type=tcp&flow=xtls-rprx-vision&sni=${sni_v}&sid=${sid}#${REMARK}"
    fi
  fi
  printf '%s' "$url"
}

SHARE_LINK=$(harvest_link)
if [ -z "$SHARE_LINK" ]; then
  sleep 3
  SHARE_LINK=$(harvest_link)
fi
[ -n "$SHARE_LINK" ] || fail "节点可能已创建，但未能读到分享链接。请在服务器执行 sb 查看，或把链接手动填回面板。"

UUID=$(jq -r --arg t "$TAG" '.[$t].share_link // empty' "$META" 2>/dev/null | sed -n 's#^vless://\([^@]*\)@.*#\1#p')
[ -n "$UUID" ] || UUID=$(jq -r --argjson p "$NODE_PORT" '.inbounds[]? | select(.listen_port == $p) | .users[0].uuid // empty' "$CONF" 2>/dev/null | head -1)
PUB=$(jq -r --arg t "$TAG" '.[$t].publicKey // empty' "$META" 2>/dev/null)
SID=$(jq -r --arg t "$TAG" '.[$t].shortId // empty' "$META" 2>/dev/null)

report "节点创建完成"
report "分享链接: $SHARE_LINK"

callback /a/result "$(cat <<JSON
{"ok":true,"share_link":$(json_escape "$SHARE_LINK"),"ip":$(json_escape "$HOST_IP"),"hostname":$(json_escape "$HOSTNAME_S"),"os":$(json_escape "$OS_INFO"),"uuid":$(json_escape "${UUID:-}"),"public_key":$(json_escape "${PUB:-}"),"short_id":$(json_escape "${SID:-}"),"tag":$(json_escape "$TAG")}
JSON
)"

install_agent
report "全部完成。可以把分享链接导入 v2rayN / sing-box / Clash Meta。"
exit 0
'''

UNINSTALL_SH = r'''#!/bin/bash
# singbox-mgmt uninstall — remove VLESS Reality inbound created by the panel
set -u
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
export DEBIAN_FRONTEND=noninteractive
umask 077

PANEL='__PANEL__'
PANEL_HOST='__PANEL_HOST__'
ORIGIN_IP='__ORIGIN_IP__'
TOKEN='__TOKEN__'
NODE_PORT='__NODE_PORT__'
CONF=/usr/local/etc/sing-box/config.json
AGENT_ENV=/usr/local/etc/singbox-mgmt-agent.env

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行。"
  exit 1
fi

curl_panel() {
  if [ -n "$ORIGIN_IP" ]; then
    curl -4 --http1.1 --tlsv1.2 --resolve "${PANEL_HOST}:443:${ORIGIN_IP}" "$@"
  else
    curl -4 --http1.1 --tlsv1.2 "$@"
  fi
}

report() {
  echo "$1"
  curl_panel -fsS -m 15 -X POST "$PANEL/a/log" \
    -H "X-Node-Token: $TOKEN" \
    -H "Content-Type: text/plain; charset=utf-8" \
    --data-binary "$1" >/dev/null 2>&1 || true
}

if [ ! -x /usr/local/bin/sb ] || [ ! -s "$CONF" ]; then
  report "本机没有 singbox-lite 节点，按已卸载处理"
  curl_panel -fsS -m 15 -X POST "$PANEL/a/gone" -H "X-Node-Token: $TOKEN" >/dev/null 2>&1 || true
  exit 0
fi

INDEX=$(jq -r --argjson p "$NODE_PORT" '
  [
    .inbounds[]?
    | select(.tag != null and .listen_port != null)
    | select((.tag | contains("-hop-")) | not)
    | select((.tag | startswith("argo-")) | not)
  ]
  | to_entries[]
  | select(.value.listen_port == $p)
  | .key + 1
' "$CONF" 2>/dev/null | head -1)

if [ -z "$INDEX" ]; then
  report "配置里没有端口 ${NODE_PORT} 的主节点，按已卸载处理"
else
  report "删除端口 ${NODE_PORT} 的节点（编号 ${INDEX}）"
  printf '4\n%s\ny\n.0\n' "$INDEX" | timeout 180 /usr/local/bin/sb >/tmp/singbox-mgmt-uninst.log 2>&1 || true
  tail -n 20 /tmp/singbox-mgmt-uninst.log 2>/dev/null | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | while IFS= read -r line; do
    [ -n "$line" ] && report "sb: $line"
  fi
fi

if [ -f "$AGENT_ENV" ]; then
  rm -f "$AGENT_ENV"
fi
if command -v crontab >/dev/null 2>&1; then
  crontab -l 2>/dev/null | grep -v singbox-mgmt-agent.env | crontab - 2>/dev/null || true
fi

curl_panel -fsS -m 15 -X POST "$PANEL/a/gone" -H "X-Node-Token: $TOKEN" >/dev/null 2>&1 || true
report "卸载命令执行完毕"
exit 0
'''


def render_script(
    template: str,
    panel: str,
    token: str,
    node_port: int,
    remark: str = "",
    sni: str = "",
    client_address: str = "",
    origin_ip: str = "",
    panel_host: str = "",
) -> str:
    from urllib.parse import urlparse

    host = panel_host or (urlparse(panel).hostname or "panel.example.com")
    return (
        template.replace("__PANEL__", panel.rstrip("/"))
        .replace("__PANEL_HOST__", host)
        .replace("__ORIGIN_IP__", origin_ip or "")
        .replace("__TOKEN__", token)
        .replace("__NODE_PORT__", str(node_port))
        .replace("__REMARK__", remark.replace("'", "'\\''"))
        .replace("__SNI__", sni)
        .replace("__CLIENT_ADDRESS__", client_address)
    )
