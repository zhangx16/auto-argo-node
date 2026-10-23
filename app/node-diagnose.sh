#!/bin/sh
# Read-only local diagnostics; never dump credentials, config files or command lines.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
if [ "$(id -u)" != 0 ];then echo '请以 root 运行';exit 1;fi
case "${1:-}" in ''|*[!0-9]*) echo '用法：sh /usr/local/lib/argo-deploy/diagnose.sh 节点ID';exit 1;; esac
DIR=/usr/local/etc/argo-deploy/node-$1
[ -r "$DIR/settings.json" ] || { echo '未找到节点配置';exit 1; }
echo 'Argo 节点本地诊断（只读，不执行修复）'
if pgrep -x sing-box >/dev/null 2>&1;then echo '当前 sing-box 进程：存在';else echo '当前 sing-box 进程：未找到';fi
jq '{node_id,protocol,port,applied_revision,health_mode,management_enabled,core_channel}' "$DIR/settings.json"
[ ! -f "$DIR/disabled" ] || echo '警告：监控已停用'
PORT=$(jq -r '.port' "$DIR/settings.json")
PROTO=$(jq -r '.protocol // "vless-ws-argo"' "$DIR/settings.json")
case "$PORT" in ''|*[!0-9]*) echo '配置端口无效';exit 1;; esac
if [ "$PROTO" = hysteria2 ];then FLAGS=-lnu;else FLAGS=-lnt;fi
if { ss "$FLAGS" 2>/dev/null || netstat "$FLAGS" 2>/dev/null; } | grep -qE ":${PORT}[[:space:]]";then echo '监听端口：存在';else echo '监听端口：未找到';fi
if (crontab -l 2>/dev/null || true) | grep -qE "# argo-deploy-node-${1}$";then echo '定时任务：已配置';else echo '定时任务：未找到';fi
if [ -r "$DIR/local-status.json" ];then
 jq '{sampled_at,state,process_alive,local_ws,public_ws,failure_count,repair_count,last_repair,panel_http_code,resources}' "$DIR/local-status.json"
else echo '暂无采样记录，请等待一分钟；旧版监控请先升级';fi
echo '时间为 Unix 秒；panel_http_code 为最近一次心跳的 HTTP 状态，000 表示连接失败。'
echo '资源为整台服务器 / 同名进程 RSS 合计；共享核心内存不按节点拆分。'

if [ -f /usr/local/etc/argo-monitor/environment ];then
 echo '独立资源探针：已安装（不输出凭据）'
 if pgrep -x monitor-agent >/dev/null 2>&1;then echo '探针进程：存在';else echo '探针进程：未找到';fi
 if [ -r "/var/lib/argo-monitor/services/$1.json" ];then
  jq '{node_id,port,protocol,revision,published_at,core}' "/var/lib/argo-monitor/services/$1.json"
 fi
else echo '独立资源探针：未安装';fi
if [ -s /usr/local/etc/sing-box/core-rollback/sing-box ];then echo '核心回滚备份：可用';else echo '核心回滚备份：未建立';fi
