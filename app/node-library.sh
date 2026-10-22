# Generated from verified singbox-lite 5486a730555ee96730791062aefd440a7b892751.
# Only three panel protocols and their operational dependency closure.
ARGO_ACTION=${1:-}
#!/bin/bash

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

# 配置、元数据、私钥和临时文件默认仅 root 可读写。
umask 077

# 基础路径定义
export SCRIPT_VERSION="28"
export DEFAULT_SNI="www.amd.com"
export WS_EARLY_DATA_SIZE="2560"
export WS_EARLY_DATA_HEADER="Sec-WebSocket-Protocol"
SELF_SCRIPT_PATH="$(readlink -f "$0")"
SCRIPT_DIR="$(dirname "$SELF_SCRIPT_PATH")"
SINGBOX_DIR="/usr/local/etc/sing-box"
SINGBOX_FIXED_VERSION="1.13.21"
SINGBOX_CORE_LOCK_FILE="${SINGBOX_DIR}/core-version.lock"
GITHUB_RAW_BASE="https://raw.githubusercontent.com/0xdabiaoge/singbox-lite/main"
SCRIPT_UPDATE_URL="${GITHUB_RAW_BASE}/singbox.sh"

# --- 核心工具函数 ---

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'
ORANGE='\033[0;33m'

# 打印消息函数
_info() { echo -e "${CYAN}[信息] $1${NC}" >&2; }
_success() { echo -e "${GREEN}[成功] $1${NC}" >&2; }
_warn() { echo -e "${YELLOW}[注意] $1${NC}" >&2; }
_warning() { _warn "$1"; } # 别名兼容
_error() { echo -e "${RED}[错误] $1${NC}" >&2; }

# Podman 容器通常没有 NET_ADMIN，nftables 转发会由 advanced_relay.sh
# 自动降级到 sing-box 用户态转发。检测到该环境时不安装不会被使用的
# 可选包，避免 128M 容器在 apk 解包时触发 OOM。
_is_podman_environment() {
    [ -f /run/.containerenv ] && return 0
    grep -qaE 'libpod|podman' /proc/1/cgroup /proc/1/environ 2>/dev/null
}

# BusyBox flock 不支持 util-linux 的 -w 参数；两者都支持 -n。
# 使用短间隔轮询实现有界等待，保证 Alpine 与 Debian 共用同一套锁语义。
_flock_wait() {
    local fd="$1" timeout="${2:-30}" attempts i
    [[ "$fd" =~ ^[0-9]+$ && "$timeout" =~ ^[0-9]+$ ]] || return 1
    attempts=$((timeout * 10))
    for ((i = 0; i <= attempts; i++)); do
        flock -n "$fd" 2>/dev/null && return 0
        ((i < attempts)) && sleep 0.1
    done
    return 1
}

# 所有脚本组件约定使用同一把状态锁。外层事务持锁时会导出
# SINGBOXLITE_LOCK_HELD=1，避免原子写函数重复加锁造成父子死锁。
_with_state_lock() {
    if [ "${SINGBOXLITE_LOCK_HELD:-0}" = "1" ]; then
        "$@"
        return $?
    fi
    if ! command -v flock &>/dev/null; then
        _error "缺少 flock，拒绝在无共享锁保护下修改配置；请先安装 util-linux。"
        return 1
    fi

    (
        mkdir -p "$SINGBOX_DIR" 2>/dev/null || exit 1
        exec 9>"${SINGBOX_DIR}/.singboxlite.lock" || exit 1
        if ! _flock_wait 9 30; then
            _error "等待 singbox-lite 状态锁超时，请稍后重试。"
            exit 1
        fi
        export SINGBOXLITE_LOCK_HELD=1
        "$@"
    )
}

_secure_state_permissions() {
    [ -d "$SINGBOX_DIR" ] && chmod 700 "$SINGBOX_DIR" 2>/dev/null || true
    local path
    for path in \
        "$CONFIG_FILE" "$CLASH_YAML_FILE" "$METADATA_FILE" "$ARGO_METADATA_FILE" \
        "${SINGBOX_DIR}/relay.json" "${SINGBOX_DIR}/relay_links.json" \
        "${SINGBOX_DIR}/relay_pf.json" "$SINGBOX_CORE_LOCK_FILE" "/usr/local/etc/xray/config.json" \
        "/usr/local/etc/xray/metadata.json"; do
        [ -f "$path" ] && chmod 600 "$path" 2>/dev/null || true
    done
    for path in "$SINGBOX_DIR"/*.key /usr/local/etc/xray/*.key; do
        [ -f "$path" ] && chmod 600 "$path" 2>/dev/null || true
    done
}

# 检查 root 权限
_check_root() {
    if [[ $EUID -ne 0 ]]; then
        _error "此脚本必须以 root 权限运行。"
        exit 1
    fi
}

# 编解码器 (纯 Bash 稳健实现)

# 公网 IP 获取 (带全局缓存)


# 系统环境检测
_detect_init_system() {
    if [ -f /sbin/openrc-run ] || command -v rc-service &>/dev/null; then
        export INIT_SYSTEM="openrc"
        export SERVICE_FILE="/etc/init.d/sing-box"
    elif command -v systemctl &>/dev/null && [ -d /run/systemd/system ]; then
        export INIT_SYSTEM="systemd"
        export SERVICE_FILE="/etc/systemd/system/sing-box.service"
    else
        export INIT_SYSTEM="direct"
        export SERVICE_FILE=""
    fi
}

# 端口占用检查
_check_port_occupied() {
    local port=$1
    local proto=${2:-tcp}
    if [[ "$proto" == "tcp" ]]; then
        if command -v ss &>/dev/null; then
            ss -lnpt | grep -q ":${port} " && return 0
        else
            netstat -lnpt | grep -q ":${port} " && return 0
        fi
    else
        if command -v ss &>/dev/null; then
            ss -lnpu | grep -q ":${port} " && return 0
        else
            netstat -lnpu | grep -q ":${port} " && return 0
        fi
    fi
    return 1
}

_is_pid_running_cmd() {
    local pid="$1"
    local pattern="$2"
    [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 1 ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [ -r "/proc/${pid}/cmdline" ]; then
        tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -Fq "$pattern"
    else
        ps -p "$pid" -o args= 2>/dev/null | grep -Fq "$pattern"
    fi
}

_is_pid_file_running_cmd() {
    local pid_file="$1"
    local pattern="$2"
    local pid
    [ -s "$pid_file" ] || return 1
    pid=$(cat "$pid_file" 2>/dev/null)
    _is_pid_running_cmd "$pid" "$pattern"
}

_prepare_singbox_runtime_pid() {
    local legacy_pid_file="/tmp/sing-box.pid"
    mkdir -p "$RUNTIME_DIR" || return 1
    chmod 700 "$RUNTIME_DIR" 2>/dev/null || return 1

    if _is_pid_file_running_cmd "$PID_FILE" "$SINGBOX_BIN"; then
        rm -f -- "$legacy_pid_file"
        return 0
    fi
    rm -f -- "$PID_FILE"

    # 仅迁移能通过 cmdline 校验的旧 PID，绝不盲信 /tmp 中的内容。
    if _is_pid_file_running_cmd "$legacy_pid_file" "$SINGBOX_BIN"; then
        local legacy_pid
        legacy_pid=$(cat "$legacy_pid_file" 2>/dev/null)
        printf '%s\n' "$legacy_pid" > "$PID_FILE" || return 1
        chmod 600 "$PID_FILE" 2>/dev/null || return 1
    fi
    rm -f -- "$legacy_pid_file"
}

# 根据 inbound 类型判断其实际监听的传输层，并允许在改端口时排除自身
# 及自身的 HY2 原生 hop 子节点。
_check_port_in_singbox_file() {
    local file="$1" port="$2" proto="${3:-tcp}" exclude_tag="${4:-}"
    [ -s "$file" ] || return 1
    jq -e --argjson port "$port" --arg proto "$proto" --arg exclude "$exclude_tag" '
        def transports:
            if (.type == "hysteria2" or .type == "tuic") then ["udp"]
            elif (.type == "shadowsocks" or .type == "mixed") then ["tcp", "udp"]
            elif .type == "direct" then
                ((.network // "tcp") | if . == "tcp+udp" then ["tcp", "udp"] else [.] end)
            else ["tcp"] end;
        .inbounds[]?
        | select((.listen_port | tonumber?) == $port)
        | select($exclude == "" or (.tag != $exclude and ((.tag // "") | startswith($exclude + "-hop-") | not)))
        | select(transports | index($proto) != null)
    ' "$file" >/dev/null 2>&1
}

_check_port_in_xray_config() {
    local port="$1" proto="${2:-tcp}"
    local xray_config="/usr/local/etc/xray/config.json"
    [ -s "$xray_config" ] || return 1
    jq -e --argjson port "$port" --arg proto "$proto" '
        def transports:
            ((.streamSettings.network // .settings.network // "tcp") | ascii_downcase) as $network
            | if ($network == "quic" or $network == "udp") then ["udp"]
              elif ($network == "tcp,udp" or $network == "tcp+udp") then ["tcp", "udp"]
              elif .protocol == "shadowsocks" then ["tcp", "udp"]
              else ["tcp"] end;
        .inbounds[]?
        | select((.port | tonumber?) == $port)
        | select(transports | index($proto) != null)
    ' "$xray_config" >/dev/null 2>&1
}

_check_port_in_pf_metadata() {
    local port="$1" proto="${2:-tcp}"
    local pf_meta="${SINGBOX_DIR}/relay_pf.json"
    [ -s "$pf_meta" ] || return 1
    jq -e --arg port "$port" --arg proto "$proto" '
        to_entries[]?
        | select(.key == $port)
        | (.value.network // "tcp") as $network
        | select($network == $proto or $network == "tcp+udp")
    ' "$pf_meta" >/dev/null 2>&1
}

# 兼容旧调用；默认检查主 sing-box 的 TCP 监听。

# 综合端口碰撞检测：系统监听、主配置、中转配置、Xray、端口转发和
# HY2 跳跃范围统一检查；TCP/UDP 分开判断。
_check_port_conflict() {
    local port="$1" requested_proto="${2:-tcp}" silent="${3:-false}" exclude_tag="${4:-}" skip_system="${5:-false}"
    if [[ ! "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        [ "$silent" != "true" ] && _error "端口 ${port} 无效，应为 1-65535。"
        return 0
    fi

    local protocols=""
    case "$requested_proto" in
        tcp) protocols="tcp" ;;
        udp) protocols="udp" ;;
        tcp+udp|udp+tcp|both) protocols="tcp udp" ;;
        *) protocols="$requested_proto" ;;
    esac

    local proto source
    for proto in $protocols; do
        source=""
        if _check_port_in_singbox_file "$CONFIG_FILE" "$port" "$proto" "$exclude_tag"; then
            source="主 sing-box 配置"
        elif _check_port_in_singbox_file "${SINGBOX_DIR}/relay.json" "$port" "$proto" ""; then
            source="中转 relay.json"
        elif _check_port_in_xray_config "$port" "$proto"; then
            source="Xray 配置"
        elif _check_port_in_pf_metadata "$port" "$proto"; then
            source="端口转发规则"
        elif [ "$skip_system" != "true" ] && _check_port_occupied "$port" "$proto"; then
            source="系统监听"
        fi
        if [ -n "$source" ]; then
            [ "$silent" != "true" ] && _error "${proto^^} 端口 ${port} 已被${source}占用。"
            return 0
        fi

        if [ "$proto" = "udp" ]; then
            local hop_conflict
            hop_conflict=$(_find_udp_hop_conflict_in_range "$port" "$port" "$exclude_tag")
            if [ -n "$hop_conflict" ]; then
                local c_tag c_name c_range c_mode
                IFS=$'\t' read -r c_tag c_name c_range c_mode <<< "$hop_conflict"
                [ "$silent" != "true" ] && _error "UDP 端口 ${port} 落在已有 HY2 端口跳跃范围 ${c_range} 内（${c_name}, ${c_tag}, ${c_mode}）。"
                return 0
            fi
        fi
    done
    return 1
}

_find_udp_hop_conflict_in_range() {
    local start="$1" end="$2" exclude_tag="${3:-}"
    local conflict=""
    if [ -f "$METADATA_FILE" ]; then
        conflict=$(jq -r --argjson start "$start" --argjson end "$end" --arg exclude "$exclude_tag" '
            to_entries[]
            | select(.key != $exclude)
            | select(.value.portHopping)
            | (.value.portHopping | capture("^(?<start>[0-9]+)-(?<end>[0-9]+)$")?) as $range
            | select($range != null)
            | ($range.start | tonumber) as $other_start
            | ($range.end | tonumber) as $other_end
            | select($start <= $other_end and $end >= $other_start)
            | [
                .key,
                (.value.name // .key),
                .value.portHopping,
                ("主HY2/" + (.value.portHoppingMode // "unknown"))
              ]
            | @tsv
        ' "$METADATA_FILE" 2>/dev/null | head -n 1)
        [ -n "$conflict" ] && { echo "$conflict"; return 0; }
    fi

    local relay_links="${SINGBOX_DIR}/relay_links.json"
    if [ -f "$relay_links" ]; then
        conflict=$(jq -r --argjson start "$start" --argjson end "$end" --arg exclude "$exclude_tag" '
            to_entries[]
            | select(.key != $exclude)
            | select(.value.port_hopping)
            | (.value.port_hopping | capture("^(?<start>[0-9]+)-(?<end>[0-9]+)$")?) as $range
            | select($range != null)
            | ($range.start | tonumber) as $other_start
            | ($range.end | tonumber) as $other_end
            | select($start <= $other_end and $end >= $other_start)
            | [
                .key,
                (.value.node_name // .key),
                .value.port_hopping,
                "中转HY2/nftables"
              ]
            | @tsv
        ' "$relay_links" 2>/dev/null | head -n 1)
        [ -n "$conflict" ] && { echo "$conflict"; return 0; }
    fi

    return 1
}

# nftables 规则管理 (独立表，避免污染系统其他防火墙规则)
export NFT_TABLE="singboxlite"
export NFT_PERSIST_FILE="/etc/nftables.d/singboxlite.nft"


# 公网 IP 初始化

# 统一服务管理
_manage_service() {
    local action="$1"

    # NTP 查询失败不等于核心必然退出，但首次查询仍有等待成本。
    # 先有界探测，再迁移脚本管理的默认配置；容器也可使用进程内时间补偿。
    if [[ "$action" == "restart" || "$action" == "start" ]]; then
        _time_prepare_ntp_config || return 1
        _ensure_relay_config || return 1
        local preflight_result
        if ! preflight_result=$(_check_combined_config_files "$SINGBOX_BIN" "$CONFIG_FILE" "$RELAY_CONFIG_FILE" 2>&1); then
            _error "拒绝启动：config.json + relay.json 组合配置无效。"
            echo "$preflight_result"
            return 1
        fi
        _secure_state_permissions
    fi

    [ -z "$INIT_SYSTEM" ] && _detect_init_system
    [ "$action" == "status" ] || _info "正在使用 ${INIT_SYSTEM} 执行: $action..."
    case "$INIT_SYSTEM" in
        systemd)
            if [ "$action" == "status" ]; then systemctl status sing-box --no-pager -l; return; fi
            # Containers and rapid successive management operations can exhaust
            # systemd's start-rate counter even when the configuration is valid.
            # Clear the counter before an explicit operator start/restart.
            if [ "$action" == "start" ] || [ "$action" == "restart" ]; then
                systemctl reset-failed sing-box >/dev/null 2>&1 || true
            fi
            systemctl "$action" sing-box 8>&- 9>&- 219>&- ;;
        openrc)
            if [ "$action" == "status" ]; then rc-service sing-box status; return; fi
            rc-service sing-box "$action" 8>&- 9>&- 219>&- ;;
        direct)
            if ! _prepare_singbox_runtime_pid; then
                _error "无法准备受保护的运行目录: ${RUNTIME_DIR}"
                return 1
            fi
            case "$action" in
                start)
                    if _is_pid_file_running_cmd "$PID_FILE" "$SINGBOX_BIN"; then
                        _success "sing-box 已在 direct 模式运行。"
                        return 0
                    fi
                    rm -f "$PID_FILE"
                    [ -s "${SINGBOX_DIR}/relay.json" ] || echo '{"inbounds":[],"outbounds":[],"route":{"rules":[]}}' > "${SINGBOX_DIR}/relay.json"
                    nohup env GOMEMLIMIT="$(_get_mem_limit)MiB" \
                        "$SINGBOX_BIN" run -c "$CONFIG_FILE" -c "${SINGBOX_DIR}/relay.json" \
                        >> "$LOG_FILE" 2>&1 8>&- 9>&- 219>&- &
                    echo $! > "$PID_FILE"
                    chmod 600 "$PID_FILE" 2>/dev/null || true
                    sleep 1
                    if ! _is_pid_file_running_cmd "$PID_FILE" "$SINGBOX_BIN"; then
                        _error "sing-box 启动后立即退出，请检查日志。"
                        rm -f "$PID_FILE"
                        return 1
                    fi
                    _success "sing-box 已以 direct 后台模式启动。"
                    ;;
                stop)
                    if [ -s "$PID_FILE" ]; then
                        local pid
                        pid=$(cat "$PID_FILE" 2>/dev/null)
                        if _is_pid_running_cmd "$pid" "$SINGBOX_BIN"; then
                            kill "$pid" 2>/dev/null
                        fi
                    fi
                    rm -f "$PID_FILE"
                    _success "sing-box direct 后台进程已停止。"
                    ;;
                restart)
                    _manage_service stop
                    sleep 1
                    _manage_service start
                    ;;
                status)
                    if _is_pid_file_running_cmd "$PID_FILE" "$SINGBOX_BIN"; then
                        _success "sing-box direct 后台模式运行中 (PID: $(cat "$PID_FILE"))"
                    else
                        rm -f "$PID_FILE"
                        _warn "sing-box direct 后台模式未运行。"
                        return 1
                    fi
                    ;;
                *) _error "direct 模式不支持的服务操作: $action"; return 1 ;;
            esac
            ;;
        *) _error "不支持的服务管理系统" ;;
    esac
}

# 智能包管理
_pkg_install() {
    local pkgs="$*"
    [ -z "$pkgs" ] && return 0
    if command -v apk &>/dev/null; then
        apk add --no-cache $pkgs >/dev/null 2>&1
    elif command -v apt-get &>/dev/null; then
        # 全新 LXC/容器上 apt 缓存可能为空，必须先 update
        if [ ! -d "/var/lib/apt/lists" ] || [ "$(ls -A /var/lib/apt/lists/ 2>/dev/null | wc -l)" -le 1 ]; then
            apt-get update -qq >/dev/null 2>&1
        fi
        # 128M Podman 中一次解析/解包整批依赖会把 apt 推到 cgroup 上限，常见结果是
        # 第一次 apt-get 被 OOM kill、第二次依靠部分安装状态侥幸成功。容器内改为逐包、
        # 禁用 recommends 和 dpkg PTY，主动压低峰值内存；普通 LXC/KVM 仍保留批量安装。
        if _is_podman_environment; then
            local pkg
            for pkg in $pkgs; do
                if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
                    continue
                fi
                if ! DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
                    -o Dpkg::Use-Pty=0 -o Acquire::Languages=none "$pkg" >/dev/null 2>&1; then
                    apt-get update -qq -o Acquire::Languages=none >/dev/null 2>&1 || return 1
                    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
                        -o Dpkg::Use-Pty=0 -o Acquire::Languages=none "$pkg" >/dev/null 2>&1 || return 1
                fi
            done
        else
            DEBIAN_FRONTEND=noninteractive apt-get install -y $pkgs >/dev/null 2>&1 || {
                # 兜底：如果安装失败，强制刷新索引后重试
                apt-get update -qq >/dev/null 2>&1
                DEBIAN_FRONTEND=noninteractive apt-get install -y $pkgs >/dev/null 2>&1
            }
        fi
    elif command -v yum &>/dev/null; then yum install -y $pkgs >/dev/null 2>&1
    elif command -v dnf &>/dev/null; then dnf install -y $pkgs >/dev/null 2>&1
    else
        return 1
    fi
}

# 原子修改 JSON/YAML 文件
_atomic_modify_json() {
    local file="$1" filter="$2"
    shift 2
    [ ! -f "$file" ] && return 1
    _with_state_lock _atomic_modify_json_locked "$file" "$filter" "$@"
}
_atomic_modify_json_locked() {
    local file="$1" filter="$2"
    shift 2
    local tmp
    tmp=$(mktemp "${file}.tmp.XXXXXX") || return 1
    if jq "$@" "$filter" "$file" > "$tmp" && jq empty "$tmp" >/dev/null 2>&1; then
        chmod 600 "$tmp" 2>/dev/null || true
        if ! mv -f "$tmp" "$file"; then
            _error "替换 JSON 失败: $file"
            rm -f "$tmp"
            return 1
        fi
    else
        _error "修改 JSON 失败: $file"
        rm -f "$tmp"
        return 1
    fi
}
_atomic_modify_yaml() {
    local file="$1" filter="$2"
    [ ! -f "$file" ] && return 1
    _with_state_lock _atomic_modify_yaml_locked "$file" "$filter"
}
_atomic_modify_yaml_locked() {
    local file="$1" filter="$2"
    local tmp
    tmp=$(mktemp "${file}.tmp.XXXXXX") || return 1
    cp "$file" "$tmp" || { rm -f "$tmp"; return 1; }
    if ${YQ_BINARY} eval "$filter" -i "$tmp" 2>/dev/null; then
        chmod 600 "$tmp" 2>/dev/null || true
        mv -f "$tmp" "$file" || { rm -f "$tmp"; return 1; }
    else
        _error "修改 YAML 失败: $file"
        rm -f "$tmp"
        return 1
    fi
}

_ensure_relay_config_locked() {
    if [ ! -s "$RELAY_CONFIG_FILE" ]; then
        local tmp
        tmp=$(mktemp "${RELAY_CONFIG_FILE}.tmp.XXXXXX") || return 1
        printf '%s\n' '{"inbounds":[],"outbounds":[],"route":{"rules":[]}}' > "$tmp"
        chmod 600 "$tmp" 2>/dev/null || true
        mv -f "$tmp" "$RELAY_CONFIG_FILE" || { rm -f "$tmp"; return 1; }
    fi
    jq empty "$RELAY_CONFIG_FILE" >/dev/null 2>&1
}

_ensure_relay_config() {
    mkdir -p "$SINGBOX_DIR" || return 1
    _with_state_lock _ensure_relay_config_locked
}

# 服务始终同时加载 config.json 和 relay.json；所有校验必须复现真实启动参数。
_check_combined_config_files() {
    local binary="${1:-$SINGBOX_BIN}" main_config="${2:-$CONFIG_FILE}" relay_config="${3:-$RELAY_CONFIG_FILE}"
    [ -x "$binary" ] || { _error "sing-box 核心不可执行: $binary"; return 1; }
    [ -s "$main_config" ] || { _error "主配置不存在或为空: $main_config"; return 1; }
    [ -s "$relay_config" ] || { _error "中转配置不存在或为空: $relay_config"; return 1; }
    "$binary" check -c "$main_config" -c "$relay_config"
}

# 将菜单中的传统 DNS 地址表示转换为 sing-box 1.12+ typed server。
# 1.14 已移除 legacy address 字段；域名上游额外使用本地 bootstrap，避免自解析循环。

# --- 资源与环境管理 ---

# 时间管理：不放宽 SS2022 的 30 秒防重放校验，不要求容器修改系统时间。
_time_is_container() {
    # Incus 虚拟机也可能提供 /dev/lxd；不能仅凭该接口判定为容器。
    [ -e /run/.containerenv ] || [ -e /.dockerenv ] || [ -s /run/systemd/container ] && return 0
    if command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt --container --quiet 2>/dev/null; then
        return 0
    fi
    grep -qaE 'container=|lxc|libpod|podman|docker' /proc/1/environ /proc/1/cgroup 2>/dev/null
}

# 只发起 SNTP 查询；Bash UDP + dd/od，避免为低内存容器安装 Python/守护进程。
# 随机 transmit nonce 必须被原样回显；检查服务器模式、闰秒状态及 stratum。
_time_probe_ntp() {
    local server="$1" port="${2:-123}" nonce escaped="" raw started finished i octet
    local -a bytes
    TIME_PROBE_EPOCH="" TIME_PROBE_OFFSET="" TIME_PROBE_SOURCE=""
    [[ "$port" =~ ^[0-9]+$ ]] && ((port > 0 && port <= 65535)) || return 1
    for i in timeout bash openssl dd od date; do command -v "$i" >/dev/null 2>&1 || return 1; done
    nonce=$(openssl rand -hex 8) || return 1
    [[ "$nonce" =~ ^[0-9a-f]{16}$ ]] || return 1
    for ((i=0; i<16; i+=2)); do escaped+="\\x${nonce:i:2}"; done
    started=$(date +%s) || return 1
    raw=$(timeout -s KILL 3 bash -c '
        exec 3<>"/dev/udp/$1/$2" || exit 1
        # UDP 必须一次写出完整的 48 字节；分次 printf/dd 会产生多个报文。
        packet="\\043"
        for ((i=0; i<39; i++)); do packet+="\\000"; done
        printf "%b" "$packet$3" >&3 || exit 1
        # BusyBox timeout 不保证杀掉整棵进程树；直接约束阻塞读取者。
        timeout -s KILL 2 dd bs=512 count=1 <&3 2>/dev/null | od -An -v -tu1
    ' bash "$server" "$port" "$escaped" 2>/dev/null) || return 1
    finished=$(date +%s) || return 1
    raw=${raw//$'\n'/ }
    read -r -a bytes <<< "$raw"
    [ "${#bytes[@]}" -ge 48 ] || return 1
    for octet in "${bytes[@]}"; do [[ "$octet" =~ ^[0-9]+$ ]] && ((octet <= 255)) || return 1; done
    (( (bytes[0] & 7) == 4 && (bytes[0] >> 6) != 3 && bytes[1] > 0 && bytes[1] < 16 )) || return 1
    for ((i=0; i<8; i++)); do
        (( bytes[24+i] == 16#${nonce:i*2:2} )) || return 1
    done
    TIME_PROBE_EPOCH=$((bytes[40]*16777216 + bytes[41]*65536 + bytes[42]*256 + bytes[43] - 2208988800))
    ((TIME_PROBE_EPOCH >= 1577836800 && finished >= started && finished-started <= 5)) || return 1
    TIME_PROBE_OFFSET=$((TIME_PROBE_EPOCH - (started+finished)/2))
    TIME_PROBE_SOURCE="$server:$port"
}

_time_has_ss2022() {
    local config
    for config in "$CONFIG_FILE" "${RELAY_CONFIG_FILE:-${SINGBOX_DIR}/relay.json}"; do
        [ -s "$config" ] || continue
        jq -e '[.inbounds[]?, .outbounds[]?] | any(.type == "shadowsocks" and ((.method // "") | startswith("2022-")))' "$config" >/dev/null 2>&1 && return 0
    done
    return 1
}

_time_prepare_ntp_config() {
    _with_state_lock _time_prepare_ntp_config_locked
}

_time_prepare_ntp_config_locked() {
    [ -s "$CONFIG_FILE" ] || return 0
    local current managed="" wanted server state_file="${SINGBOX_DIR}/time-sync.json"
    local status="unreachable" tmp old_present
    current=$(jq -cS '.ntp // null' "$CONFIG_FILE") || return 1
    old_present=$(jq -r 'has("ntp")' "$CONFIG_FILE") || return 1
    [ ! -s "$state_file" ] || managed=$(jq -cS '.managed_ntp // null' "$state_file" 2>/dev/null)
    # 仅迁移无配置、历史默认项或与 sidecar 完全一致的脚本管理项。
    # 用户自定义服务器、间隔、detour 和显式 enabled:false 不被覆盖。
    if [ "$current" != null ] && [ "$current" != "$managed" ] && ! jq -e '
        .ntp == {enabled:true,server:"time.apple.com",server_port:123,interval:"30m"}
        or .ntp == {enabled:true,server:"time.apple.com",server_port:123,interval:"1m",write_to_system:false,connect_timeout:"3s"}
    ' "$CONFIG_FILE" >/dev/null; then
        if _time_has_ss2022; then
            _info "保留用户自定义 NTP 配置；SS2022 需确认校时成功，主菜单 [11] 可诊断。"
            if ! jq -e '.ntp.enabled == true' "$CONFIG_FILE" >/dev/null; then
                _warn "内置 NTP 已由用户关闭：SS2022 仍依赖系统时间，不能自动补偿宿主机偏差。"
            fi
        fi
        return 0
    fi

    wanted='{"enabled":false}'
    for server in time.apple.com time.cloudflare.com time.google.com; do
        if _time_probe_ntp "$server" 123; then
            wanted=$(jq -cn --arg s "$server" '{enabled:true,server:$s,server_port:123,interval:"1m",write_to_system:false,connect_timeout:"3s"}') || return 1
            status="reachable"
            break
        fi
    done
    tmp=$(mktemp "${state_file}.tmp.XXXXXX") || return 1
    if ! jq -n --argjson ntp "$wanted" --arg status "$status" --arg offset "${TIME_PROBE_OFFSET:-}" --arg source "${TIME_PROBE_SOURCE:-}" --arg checked "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        '{managed_ntp:$ntp,last_probe_status:$status,system_offset_seconds:$offset,source:$source,checked_at_system_utc:$checked}' > "$tmp" || ! chmod 600 "$tmp"; then
        rm -f -- "$tmp"
        return 1
    fi
    if [ "$current" != "$(printf '%s' "$wanted" | jq -cS .)" ]; then
        if ! _atomic_modify_json "$CONFIG_FILE" '.ntp = $ntp' --argjson ntp "$wanted"; then
            rm -f -- "$tmp"
            return 1
        fi
    fi
    if ! mv -f "$tmp" "$state_file"; then
        _atomic_modify_json "$CONFIG_FILE" 'if $present then .ntp=$ntp else del(.ntp) end' --argjson ntp "$current" --argjson present "$old_present" || _error "恢复原 NTP 配置失败"
        rm -f -- "$tmp"
        return 1
    fi
    if [ "$status" = reachable ]; then
        _info "核心 NTP: ${TIME_PROBE_SOURCE}，每 1 分钟补偿时间；系统偏差约 ${TIME_PROBE_OFFSET} 秒，不写系统时钟。"
    else
        _warn "三个 NTP 时间源均未通过有界探测，暂不启用脚本管理的内置 NTP，避免拖延其他节点启动。"
        _warn "下次启动/重启或 [11] 修复会重新探测；未校时不等于 SS2022 已可用。"
        _time_has_ss2022 && _warn "检测到 SS2022：系统偏差超过 30 秒时将被拒绝；请使用可达时间源或联系宿主机管理员。"
    fi
    return 0
}


# 自动调用（如 Argo）在容器内只提示，不安装校时软件、更不修改宿主机时间。
_sync_system_time() {
    if _time_is_container; then
        _warn "容器不执行系统校时；SS2022 使用 sing-box 内置 NTP，其他进程仍依赖宿主机时间。"
        return 0
    fi
    local caps sync_ok=false
    caps=$(awk '/^CapEff:/ {print $2}' /proc/self/status)
    if ! [[ "$caps" =~ ^[0-9a-fA-F]+$ ]] || (( (16#$caps & (1 << 25)) == 0 )); then
        _warn "缺少 CAP_SYS_TIME，不尝试修改系统时间。"
        return 1
    fi
    # 先复用运行中的持续校时服务；仅在需要时使用已有依赖安装路径。
    if command -v chronyc >/dev/null 2>&1 && timeout 5 chronyc tracking >/dev/null 2>&1; then
        timeout 8 chronyc makestep >/dev/null 2>&1 && sync_ok=true
    elif _pkg_install ntpdate >/dev/null 2>&1 && command -v ntpdate >/dev/null 2>&1; then
        timeout 10 ntpdate -u time.cloudflare.com >/dev/null 2>&1 && sync_ok=true
    elif [ "${INIT_SYSTEM:-}" = openrc ] && _pkg_install chrony >/dev/null 2>&1; then
        timeout 10 chronyd -q 'server time.cloudflare.com iburst' >/dev/null 2>&1 && sync_ok=true
    fi
    if [ "$sync_ok" != true ]; then
        _error "系统校时失败，不能宣称已同步；请检查权限、时间源及网络。"
        return 1
    fi
    if _time_probe_ntp time.cloudflare.com 123 && ((TIME_PROBE_OFFSET <= 5 && TIME_PROBE_OFFSET >= -5)); then
        _success "系统校时后复核通过：偏差约 ${TIME_PROBE_OFFSET} 秒。"
    else
        _warn "校时命令已执行，但复核未通过；建议配置持续校时服务并排查底层时钟。"
        return 1
    fi
}


# Clash YAML 节点管理
_add_node_to_yaml() {
    local proxy_json="$1"
    _with_state_lock _add_node_to_yaml_locked "$proxy_json"
}
_add_node_to_yaml_locked() {
    local proxy_json="$1"
    local proxy_name
    proxy_name=$(echo "$proxy_json" | jq -r .name)
    [ -n "$proxy_name" ] && [ "$proxy_name" != "null" ] || { _error "节点名称为空，拒绝写入共享 YAML。"; return 1; }
    export PROXY_NAME="$proxy_name"
    if ${YQ_BINARY} eval '.proxies[] | select(.name == env(PROXY_NAME)) | .name' "$CLASH_YAML_FILE" 2>/dev/null | grep -Fxq "$proxy_name"; then
        _error "共享 Clash YAML 已存在同名节点 [$proxy_name]；为避免跨模块覆盖，请更换名称。"
        return 1
    fi
    if ! ${YQ_BINARY} eval '.proxy-groups[] | select(.name == "节点选择") | .name' "$CLASH_YAML_FILE" 2>/dev/null | grep -Fxq '节点选择'; then
        _error "共享 Clash YAML 缺少 [节点选择] 分组，拒绝写入孤立代理。"
        return 1
    fi
    local yaml_backup
    yaml_backup=$(mktemp "${CLASH_YAML_FILE}.add.backup.XXXXXX") || return 1
    cp -p "$CLASH_YAML_FILE" "$yaml_backup" || { rm -f "$yaml_backup"; return 1; }
    _atomic_modify_yaml "$CLASH_YAML_FILE" ".proxies |= . + [${proxy_json}]" || { mv -f "$yaml_backup" "$CLASH_YAML_FILE"; return 1; }
    export PROXY_NAME="$proxy_name"
    if ! _atomic_modify_yaml "$CLASH_YAML_FILE" '(.proxy-groups[] | select(.name == "节点选择") | .proxies) = ((((.proxy-groups[] | select(.name == "节点选择") | .proxies) // []) + [env(PROXY_NAME)]) | unique)'; then
        mv -f "$yaml_backup" "$CLASH_YAML_FILE"
        return 1
    fi
    rm -f "$yaml_backup"
}
_remove_node_from_yaml() {
    local proxy_name="$1"
    _with_state_lock _remove_node_from_yaml_locked "$proxy_name"
}
_remove_node_from_yaml_locked() {
    local proxy_name="$1"
    export PROXY_NAME="$proxy_name"
    local yaml_backup
    yaml_backup=$(mktemp "${CLASH_YAML_FILE}.remove.backup.XXXXXX") || return 1
    cp -p "$CLASH_YAML_FILE" "$yaml_backup" || { rm -f "$yaml_backup"; return 1; }
    _atomic_modify_yaml "$CLASH_YAML_FILE" 'del(.proxies[] | select(.name == env(PROXY_NAME)))' || { mv -f "$yaml_backup" "$CLASH_YAML_FILE"; return 1; }
    if ! _atomic_modify_yaml "$CLASH_YAML_FILE" '(.proxy-groups[] | .proxies) |= (((. // []) | map(select(. != env(PROXY_NAME)))))'; then
        mv -f "$yaml_backup" "$CLASH_YAML_FILE"
        return 1
    fi
    rm -f "$yaml_backup"
}

# 仅列出主脚本可管理的外层节点：排除 Argo、HY2 hop 以及 ShadowTLS 内层。

# 内存限额计算
_get_mem_limit() {
    local total_mem_mb=0 cgroup_limit="" candidate_mb=0
    local limit limit_file

    total_mem_mb=$(awk '/^MemTotal:/{print int($2 / 1024); exit}' /proc/meminfo 2>/dev/null)
    [[ "$total_mem_mb" =~ ^[0-9]+$ ]] || total_mem_mb=0

    # cgroup v2 的 memory.high 也可能比 memory.max 更早触发回收；取系统
    # 可见内存、memory.max 和 memory.high 中最小的有效值，兼容嵌套容器。
    if [ -r /sys/fs/cgroup/memory.max ]; then
        for limit_file in /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.high; do
            [ -r "$limit_file" ] || continue
            cgroup_limit=$(cat "$limit_file" 2>/dev/null)
            if [[ "$cgroup_limit" =~ ^[0-9]+$ ]] && [ "$cgroup_limit" -lt 9223372036854771712 ] 2>/dev/null; then
                candidate_mb=$((cgroup_limit / 1024 / 1024))
                if [ "$candidate_mb" -gt 0 ] && { [ "$total_mem_mb" -eq 0 ] || [ "$candidate_mb" -lt "$total_mem_mb" ]; }; then
                    total_mem_mb=$candidate_mb
                fi
            fi
        done
    elif [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
        cgroup_limit=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null)
        if [[ "$cgroup_limit" =~ ^[0-9]+$ ]] && [ "$cgroup_limit" -lt 9223372036854771712 ] 2>/dev/null; then
            candidate_mb=$((cgroup_limit / 1024 / 1024))
            if [ "$candidate_mb" -gt 0 ] && { [ "$total_mem_mb" -eq 0 ] || [ "$candidate_mb" -lt "$total_mem_mb" ]; }; then
                total_mem_mb=$candidate_mb
            fi
        fi
    fi

    [ "$total_mem_mb" -gt 0 ] || total_mem_mb=128

    if [ "$total_mem_mb" -le 128 ]; then
        # 128MB 机器保留更多空间给内核、TLS、socket 缓冲和测速客户端。
        limit=$((total_mem_mb * 5 / 16))
    elif [ "$total_mem_mb" -le 256 ]; then
        limit=$((total_mem_mb * 50 / 100))
    elif [ "$total_mem_mb" -le 512 ]; then
        limit=$((total_mem_mb * 65 / 100))
    else
        limit=$((total_mem_mb * 80 / 100))
    fi

    [ "$limit" -lt 32 ] && limit=32
    echo "$limit"
}

# 页缓存由内核按内存压力自动回收。容器中 /proc/sys/vm/drop_caches 可能
# 意外映射到宿主机全局开关，主动写入会影响整台宿主机，甚至触发容器重启。
_release_install_cache() {
    sync 2>/dev/null || true
    return 0
}

# 安装 yq
_install_yq() {
    if [ ! -x "$YQ_BINARY" ] || ! "$YQ_BINARY" --version >/dev/null 2>&1; then
        _info "安装 yq..."
        local arch
        arch=$(uname -m)
        case $arch in
            x86_64|amd64) arch='amd64' ;;
            aarch64|arm64) arch='arm64' ;;
            armv7l|armv7|armhf) arch='arm' ;;
            *) _error "yq 不支持当前架构: $(uname -m)"; return 1 ;;
        esac
        local asset="yq_linux_${arch}"
        local release_json release_tag release_base
        release_json=$(curl -fsSL --max-time 15 https://api.github.com/repos/mikefarah/yq/releases/latest 2>/dev/null) || true
        release_tag=$(printf '%s' "$release_json" | jq -r '.tag_name // empty' 2>/dev/null)
        if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            _error "无法解析 yq 官方 release tag。"
            return 1
        fi
        release_base="https://github.com/mikefarah/yq/releases/download/${release_tag}"

        local tmp_yq checksums_tmp order_tmp
        tmp_yq=$(mktemp "$(dirname "$YQ_BINARY")/.yq.new.XXXXXX") || return 1
        checksums_tmp=$(mktemp /tmp/singboxlite-yq-checksums.XXXXXX) || { rm -f -- "$tmp_yq"; return 1; }
        order_tmp=$(mktemp /tmp/singboxlite-yq-order.XXXXXX) || { rm -f -- "$tmp_yq" "$checksums_tmp"; return 1; }
        if ! wget -qO "$tmp_yq" "${release_base}/${asset}" \
            || ! wget -qO "$checksums_tmp" "${release_base}/checksums" \
            || ! wget -qO "$order_tmp" "${release_base}/checksums_hashes_order"; then
            rm -f -- "$tmp_yq" "$checksums_tmp" "$order_tmp"
            _error "yq 或官方校验文件下载失败。"
            return 1
        fi

        local sha_line sha_field expected_sha actual_sha
        sha_line=$(grep -n -x -m 1 'SHA-256' "$order_tmp" | cut -d: -f1)
        if ! [[ "$sha_line" =~ ^[0-9]+$ ]]; then
            rm -f -- "$tmp_yq" "$checksums_tmp" "$order_tmp"
            _error "yq 官方校验文件缺少 SHA-256 定义。"
            return 1
        fi
        sha_field=$((sha_line + 1))
        expected_sha=$(awk -v name="$asset" -v field="$sha_field" '
            $1 == name { count++; value=$field }
            END { if (count == 1) print tolower(value) }
        ' "$checksums_tmp")
        actual_sha=$(openssl dgst -sha256 "$tmp_yq" 2>/dev/null | awk '{print tolower($NF)}')
        if [[ ! "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || [ "$actual_sha" != "$expected_sha" ]; then
            rm -f -- "$tmp_yq" "$checksums_tmp" "$order_tmp"
            _error "yq SHA-256 校验失败，拒绝安装。"
            return 1
        fi
        rm -f -- "$checksums_tmp" "$order_tmp"

        if ! chmod 755 "$tmp_yq" || ! "$tmp_yq" --version >/dev/null 2>&1 \
            || ! mv -f "$tmp_yq" "$YQ_BINARY"; then
            rm -f -- "$tmp_yq"
            _error "yq 下载或完整性自检失败。"
            return 1
        fi
    fi
}

# --- 核心变量定义 ---
export SINGBOX_DIR="/usr/local/etc/sing-box"
export SINGBOX_BIN="/usr/local/bin/sing-box"
export SINGBOX_FIXED_VERSION="1.13.21"
export SINGBOX_CORE_LOCK_FILE="${SINGBOX_DIR}/core-version.lock"
export YQ_BINARY="/usr/local/bin/yq"
export CONFIG_FILE="${SINGBOX_DIR}/config.json"
export RELAY_CONFIG_FILE="${SINGBOX_DIR}/relay.json"
export CLASH_YAML_FILE="${SINGBOX_DIR}/clash.yaml"
export METADATA_FILE="${SINGBOX_DIR}/metadata.json"
export ARGO_METADATA_FILE="${SINGBOX_DIR}/argo_metadata.json"
export LOG_FILE="/var/log/sing-box.log"
export ARGO_LOG_FILE="/var/log/singbox_argo.log"
export RUNTIME_DIR="/run/singboxlite"
export PID_FILE="${RUNTIME_DIR}/sing-box.pid"
export CLOUDFLARED_BIN="/usr/local/bin/cloudflared"
export XRAY_RUNTIME_DIR="${RUNTIME_DIR}"
export XRAY_PID_FILE="${XRAY_RUNTIME_DIR}/xray.pid"
export DEP_STATE_FILE="${SINGBOX_DIR}/dependencies.ok"
export DEP_STATE_VERSION="panel-three-protocol-v1"
_detect_init_system
case "$INIT_SYSTEM" in
    openrc) export SERVICE_FILE="/etc/init.d/sing-box" ;;
    systemd) export SERVICE_FILE="/etc/systemd/system/sing-box.service" ;;
    *) export SERVICE_FILE="" ;;
esac

export -f _info _success _warn _warning _error _flock_wait _detect_init_system _sync_system_time _release_install_cache _atomic_modify_json _atomic_modify_json_locked _atomic_modify_yaml _atomic_modify_yaml_locked _with_state_lock _manage_service _pkg_install _add_node_to_yaml _add_node_to_yaml_locked _remove_node_from_yaml _remove_node_from_yaml_locked

export -f _time_is_container _time_probe_ntp _time_has_ss2022 _time_prepare_ntp_config _time_prepare_ntp_config_locked

server_ip=""
BATCH_MODE=false
VIEW_LINKS_TMP=""
_cleanup_main_temp_files() {
    [ -z "${VIEW_LINKS_TMP:-}" ] || rm -f -- "$VIEW_LINKS_TMP"
}
trap _cleanup_main_temp_files EXIT
# 依赖安装
_install_dependencies() {
    local force="${1:-false}"
    if [ "$force" != "true" ] && [ -s "$DEP_STATE_FILE" ] && grep -qx "$DEP_STATE_VERSION" "$DEP_STATE_FILE" 2>/dev/null; then
        local missing_cached=""
        for cmd in bash jq curl wget openssl tar flock; do
            if ! command -v "$cmd" &>/dev/null; then
                missing_cached="$missing_cached $cmd"
            fi
        done
        if false; then
            missing_cached="$missing_cached nftables"
        fi
        if [ -z "$missing_cached" ] && [ -x "$YQ_BINARY" ]; then
            return 0
        fi
        [ ! -x "$YQ_BINARY" ] && missing_cached="$missing_cached yq"
        _warn "依赖缓存存在，但关键工具缺失:${missing_cached}，将执行一次修复安装。"
    fi

    # 核心依赖：脚本运行的绝对前提，必须全部装上
    local lock_pkg="util-linux"
    command -v apk &>/dev/null && lock_pkg="util-linux-misc"
    local core_pkgs="bash curl jq openssl wget tar ca-certificates ${lock_pkg}"
    # 可选依赖：部分功能需要，即使装失败也不致命
    local optional_pkgs="procps iproute2"

    # Podman 的网络能力由容器运行时决定，即使安装 nftables 也无法写入
    # 主机 netfilter；在容器中跳过整组可选包，只保留脚本核心依赖。
    if _is_podman_environment; then
        optional_pkgs=""
        _info "检测到 Podman 容器：跳过 nftables 等可选依赖，使用 sing-box 用户态转发"
    fi

    # 针对不同发行版的 cron 包名适配
    if command -v apk &>/dev/null; then
        optional_pkgs="${optional_pkgs/cron/dcron}"
    elif ! command -v apt-get &>/dev/null && ! command -v yum &>/dev/null && ! command -v dnf &>/dev/null; then
        optional_pkgs="${optional_pkgs/cron/cronie}"
    fi

    _info "正在安装核心依赖..."
    _pkg_install $core_pkgs
    if ! command -v flock &>/dev/null; then
        # Alpine 版本间拆包名称有差异；其余发行版均由 util-linux 提供。
        _pkg_install util-linux 2>/dev/null || true
    fi

    if [ -n "$optional_pkgs" ]; then
        _info "正在安装可选依赖..."
        _pkg_install $optional_pkgs 2>/dev/null || {
            # 可选依赖批量安装失败时逐个尝试
            _warn "部分可选依赖批量安装遇到冲突，正在逐个重试..."
            for pkg in $optional_pkgs; do
                _pkg_install "$pkg" 2>/dev/null || true
            done
        }
    fi

    _install_yq

    # [修复] Alpine 上 dcron 安装后需手动启动 cron 守护进程
    if command -v apk &>/dev/null; then
        if command -v crond &>/dev/null; then
            # BusyBox 自带 crond 并不代表系统存在 dcron 的 OpenRC 服务；
            # 定时任务守护进程属于可选能力，不能让核心安装继承失败状态。
            rc-service dcron start 2>/dev/null || true
            rc-update add dcron default 2>/dev/null || true
        fi
    fi

    # 关键依赖验证：如果核心工具缺失则无法继续
    local missing=""
    for cmd in bash jq curl wget openssl tar flock; do
        if ! command -v "$cmd" &>/dev/null; then
            missing="$missing $cmd"
        fi
    done
    if [ ! -x "$YQ_BINARY" ]; then
        missing="$missing yq"
    fi
    if [ -n "$missing" ]; then
        _error "以下关键依赖安装失败:${missing}"
        _error "请使用系统包管理器手动安装这些工具（如 apk add / apt-get install / yum install）"
        exit 1
    fi

    mkdir -p "$SINGBOX_DIR"
    printf '%s\n' "$DEP_STATE_VERSION" > "$DEP_STATE_FILE"
}

# 确保 nftables 可用，并检测实际 netfilter 写入能力
_singbox_core_is_locked() {
    [ -s "$SINGBOX_CORE_LOCK_FILE" ]
}

_write_singbox_core_lock() {
    local tmp
    mkdir -p "$SINGBOX_DIR" || return 1
    tmp=$(mktemp "${SINGBOX_CORE_LOCK_FILE}.tmp.XXXXXX") || return 1
    if ! printf '%s\n' "$SINGBOX_FIXED_VERSION" > "$tmp" \
        || ! chmod 600 "$tmp" \
        || ! mv -f "$tmp" "$SINGBOX_CORE_LOCK_FILE"; then
        rm -f -- "$tmp"
        return 1
    fi
}

_install_sing_box() {
    local requested_version="${1:-latest}"
    local api_url requested_label
    case "$requested_version" in
        latest)
            api_url="https://api.github.com/repos/SagerNet/sing-box/releases/latest"
            requested_label="最新稳定版"
            ;;
        "$SINGBOX_FIXED_VERSION")
            api_url="https://api.github.com/repos/SagerNet/sing-box/releases/tags/v${SINGBOX_FIXED_VERSION}"
            requested_label="固定版 v${SINGBOX_FIXED_VERSION}"
            ;;
        *)
            _error "不允许安装未授权的 sing-box 版本: ${requested_version}"
            return 1
            ;;
    esac
    _info "正在安装 ${requested_label} sing-box..."
    local arch=$(uname -m)
    local arch_tag
    case $arch in
        x86_64|amd64) arch_tag='amd64' ;;
        aarch64|arm64) arch_tag='arm64' ;;
        armv7l) arch_tag='armv7' ;;
        *) _error "不支持的架构：$arch"; return 1 ;;
    esac

    # 检测 C 库类型：Alpine 等系统使用 musl，需要下载对应版本
    local libc_suffix=""
    if ldd --version 2>&1 | grep -qi musl || [ -f /etc/alpine-release ]; then
        _info "检测到 musl libc (Alpine 等系统)，将下载 musl 版本..."
        libc_suffix="-musl"
    fi

    local search_pattern="linux-${arch_tag}${libc_suffix}.tar.gz"
    local asset_info release_tag release_draft release_prerelease
    local asset_count asset_name download_url asset_digest release_version expected_asset_name
    if ! asset_info=$(curl -fsSL --retry 3 --connect-timeout 10 "$api_url" | jq -r --arg pattern "$search_pattern" '
        [.assets[] | select(.name | endswith($pattern))] as $matches
        | [(.tag_name // ""),
           (if (.draft | type) == "boolean" then .draft else true end),
           (if (.prerelease | type) == "boolean" then .prerelease else true end),
           ($matches | length), ($matches[0].name // ""),
           ($matches[0].browser_download_url // ""), ($matches[0].digest // "")]
        | @tsv
    '); then
        _error "无法读取 sing-box 官方发布信息。"
        return 1
    fi
    IFS=$'\t' read -r release_tag release_draft release_prerelease asset_count asset_name download_url asset_digest <<< "$asset_info"
    if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || [ "$release_draft" != "false" ] || [ "$release_prerelease" != "false" ]; then
        _error "sing-box 官方发布元数据无效或不是稳定版，已拒绝安装。"
        return 1
    fi
    release_version="${release_tag#v}"
    if [ "$requested_version" != "latest" ] && [ "$release_version" != "$requested_version" ]; then
        _error "固定版响应不匹配：请求 v${requested_version}，实际为 ${release_tag}。"
        return 1
    fi
    if [ "$asset_count" != "1" ]; then
        _error "sing-box 官方发布中目标资产数量异常 (${asset_count:-无法解析})，已拒绝安装。"
        return 1
    fi
    expected_asset_name="sing-box-${release_version}-linux-${arch_tag}${libc_suffix}.tar.gz"
    if [ "$asset_name" != "$expected_asset_name" ] || [ -z "$download_url" ] \
        || [[ "$download_url" != "https://github.com/SagerNet/sing-box/releases/download/${release_tag}/${expected_asset_name}" ]]; then
        _error "无法获取可信的 sing-box 下载链接 (搜索: ${search_pattern})。"
        return 1
    fi
    if [[ ! "$asset_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        _error "sing-box 官方资产缺少有效的 API SHA-256 摘要，已拒绝替换核心。"
        return 1
    fi

    local temp_dir extracted_bin expected actual staged_bin digest_file extracted_version
    mkdir -p /var/tmp || { _error "创建安装临时目录失败。"; return 1; }
    # 上一次安装若被 OOM/SIGKILL 终止，退出清理不会执行。只清理本脚本
    # 使用的精确目录，避免残留的 90MB 二进制继续挤占容器内存配额。
    local stale_dir stale_pid
    for stale_dir in /var/tmp/.singbox-install.*; do
        [ -d "$stale_dir" ] || continue
        stale_pid=""
        [ -s "${stale_dir}/.active" ] && stale_pid=$(cat "${stale_dir}/.active" 2>/dev/null)
        if [ -n "$stale_pid" ] && kill -0 "$stale_pid" 2>/dev/null; then
            continue
        fi
        rm -rf -- "$stale_dir"
    done
    temp_dir=$(mktemp -d /var/tmp/.singbox-install.XXXXXX) || { _error "创建临时目录失败。"; return 1; }
    printf '%s\n' "$$" > "${temp_dir}/.active"
    digest_file="${temp_dir}/.archive.sha256"

    _info "正在下载并校验 sing-box 安装包..."
    if command -v sha256sum &>/dev/null; then
        wget -qO- "$download_url" | tee >(sha256sum > "$digest_file") | tar -xzf - -C "$temp_dir"
    else
        wget -qO- "$download_url" | tee >(openssl dgst -sha256 > "$digest_file") | tar -xzf - -C "$temp_dir"
    fi
    local -a stream_status=("${PIPESTATUS[@]}")
    if [ "${stream_status[0]:-1}" -ne 0 ] || [ "${stream_status[2]:-1}" -ne 0 ]; then
        _error "sing-box 安装包下载失败。"
        rm -rf "$temp_dir"
        return 1
    fi
    local digest_wait=0
    while [ ! -s "$digest_file" ] && [ "$digest_wait" -lt 50 ]; do
        sleep 0.1
        digest_wait=$((digest_wait + 1))
    done
    expected="${asset_digest#sha256:}"
    if command -v sha256sum &>/dev/null; then
        actual=$(awk '{print $1}' "$digest_file" 2>/dev/null)
    else
        actual=$(awk '{print $NF}' "$digest_file" 2>/dev/null)
    fi
    if [ -z "$expected" ] || [ -z "$actual" ] || [ "${expected,,}" != "${actual,,}" ]; then
        _error "sing-box 安装包 SHA-256 校验失败，已拒绝替换核心。"
        rm -rf "$temp_dir"
        return 1
    fi

    extracted_bin=$(find "$temp_dir" -name sing-box -type f 2>/dev/null | head -n 1)
    if [ -z "$extracted_bin" ]; then
        _error "解压后未找到 sing-box 二进制文件。"
        rm -rf "$temp_dir"
        return 1
    fi
    chmod 755 "$extracted_bin" || { rm -rf "$temp_dir"; return 1; }
    if ! "$extracted_bin" version >/dev/null 2>&1; then
        _error "下载的 sing-box 核心无法执行，已拒绝安装。"
        rm -rf "$temp_dir"
        return 1
    fi
    extracted_version=$("$extracted_bin" version 2>/dev/null | sed -n 's/^sing-box version \([^[:space:]]*\).*/\1/p' | head -n 1)
    if [ "$extracted_version" != "$release_version" ]; then
        _error "下载核心版本与官方发布不匹配：期望 v${release_version}，实际 v${extracted_version:-未知}。"
        rm -rf "$temp_dir"
        return 1
    fi

    # 在触碰旧核心之前，用新核心校验真实的双配置组合。
    if [ -s "$CONFIG_FILE" ]; then
        local validation_relay="$RELAY_CONFIG_FILE"
        if [ ! -s "$validation_relay" ]; then
            validation_relay="${temp_dir}/relay.json"
            printf '%s\n' '{"inbounds":[],"outbounds":[],"route":{"rules":[]}}' > "$validation_relay"
        fi
        local validation_result
        if ! validation_result=$(_check_combined_config_files "$extracted_bin" "$CONFIG_FILE" "$validation_relay" 2>&1); then
            _error "新核心无法加载当前 config.json + relay.json，已取消更新："
            echo "$validation_result"
            rm -rf "$temp_dir"
            return 1
        fi
    fi

    mkdir -p "$(dirname "$SINGBOX_BIN")" || { rm -rf "$temp_dir"; return 1; }
    staged_bin=$(mktemp "$(dirname "$SINGBOX_BIN")/.sing-box.new.XXXXXX") || { rm -rf "$temp_dir"; return 1; }
    rm -f -- "$staged_bin"
    if ! mv -f -- "$extracted_bin" "$staged_bin"; then
        # /var/tmp 与安装目录通常位于同一文件系统；跨文件系统时再退回
        # cp，保证原子替换流程仍可用。
        if ! cp -- "$extracted_bin" "$staged_bin"; then
            rm -f -- "$staged_bin"; rm -rf -- "$temp_dir"; return 1
        fi
    fi
    chmod 755 "$staged_bin" || { rm -f -- "$staged_bin"; rm -rf -- "$temp_dir"; return 1; }

    SINGBOX_UPDATE_BACKUP=""
    SINGBOX_UPDATE_HAD_OLD="false"
    if [ -f "$SINGBOX_BIN" ]; then
        SINGBOX_UPDATE_HAD_OLD="true"
        SINGBOX_UPDATE_BACKUP="$(dirname "$SINGBOX_BIN")/.sing-box.rollback.$$"
        cp -p "$SINGBOX_BIN" "$SINGBOX_UPDATE_BACKUP" || {
            rm -f "$staged_bin"; rm -rf "$temp_dir"; return 1;
        }
    fi
    if ! mv -f "$staged_bin" "$SINGBOX_BIN"; then
        rm -f "$staged_bin" "$SINGBOX_UPDATE_BACKUP"
        rm -rf "$temp_dir"
        return 1
    fi

    rm -rf "$temp_dir"
    _release_install_cache
    SINGBOX_STAGED_VERSION="$release_version"
    _success "sing-box v${release_version} 已安全暂存: ${SINGBOX_BIN}"
}

_rollback_singbox_binary() {
    if [ "${SINGBOX_UPDATE_HAD_OLD:-false}" = "true" ] && [ -s "${SINGBOX_UPDATE_BACKUP:-}" ]; then
        mv -f "$SINGBOX_UPDATE_BACKUP" "$SINGBOX_BIN" && chmod 755 "$SINGBOX_BIN"
    elif [ "${SINGBOX_UPDATE_HAD_OLD:-false}" != "true" ]; then
        rm -f "$SINGBOX_BIN"
    fi
    SINGBOX_UPDATE_BACKUP=""
}

_commit_singbox_binary() {
    [ -n "${SINGBOX_UPDATE_BACKUP:-}" ] && rm -f "$SINGBOX_UPDATE_BACKUP"
    SINGBOX_UPDATE_BACKUP=""
}

_install_cloudflared() {
    if [ -x "${CLOUDFLARED_BIN}" ] && "${CLOUDFLARED_BIN}" --version >/dev/null 2>&1; then
        _info "cloudflared 已安装: $(${CLOUDFLARED_BIN} --version 2>&1 | head -n1)"
        return 0
    fi

    _info "正在安装依据环境所需的组件 (ca-certificates)..."
    _pkg_install ca-certificates # 关键修复：Alpine 等精简系统必须有证书才能进行 TLS 握手

    _info "正在安装 cloudflared..."
    local arch=$(uname -m)
    local arch_tag
    case $arch in
        x86_64|amd64) arch_tag='amd64' ;;
        aarch64|arm64) arch_tag='arm64' ;;
        armv7l) arch_tag='arm' ;;
        *) _error "不支持的架构：$arch"; return 1 ;;
    esac

    local asset_name="cloudflared-linux-${arch_tag}"
    local asset_count asset_info download_url asset_digest expected actual
    if ! asset_info=$(curl -fsSL --retry 3 --connect-timeout 10 https://api.github.com/repos/cloudflare/cloudflared/releases/latest | jq -r --arg name "$asset_name" '
        [.assets[] | select(.name == $name)] as $matches
        | [($matches | length), ($matches[0].browser_download_url // ""), ($matches[0].digest // "")]
        | @tsv
    '); then
        _error "无法读取 cloudflared 官方发布信息。"
        return 1
    fi
    IFS=$'\t' read -r asset_count download_url asset_digest <<< "$asset_info"
    if [ "$asset_count" != "1" ]; then
        _error "cloudflared 官方发布中目标资产数量异常 (${asset_count:-无法解析})。"
        return 1
    fi
    if [[ "$download_url" != https://github.com/cloudflare/cloudflared/releases/download/* ]] \
        || [[ ! "$asset_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        _error "cloudflared 官方资产缺少可信下载地址或 API SHA-256 摘要，已拒绝安装。"
        return 1
    fi

    mkdir -p "$(dirname "$CLOUDFLARED_BIN")" || return 1
    local tmp_cloudflared
    tmp_cloudflared=$(mktemp "$(dirname "$CLOUDFLARED_BIN")/.cloudflared.new.XXXXXX") || return 1
    if ! wget -qO "$tmp_cloudflared" "$download_url"; then
        rm -f "$tmp_cloudflared"
        _error "cloudflared 下载失败!"
        return 1
    fi
    expected="${asset_digest#sha256:}"
    if command -v sha256sum &>/dev/null; then
        actual=$(sha256sum "$tmp_cloudflared" | awk '{print $1}')
    else
        actual=$(openssl dgst -sha256 "$tmp_cloudflared" 2>/dev/null | awk '{print $NF}')
    fi
    if [ -z "$actual" ] || [ "${actual,,}" != "$expected" ]; then
        rm -f -- "$tmp_cloudflared"
        _error "cloudflared SHA-256 校验失败，已拒绝安装。"
        return 1
    fi
    chmod 755 "$tmp_cloudflared" || { rm -f "$tmp_cloudflared"; return 1; }
    if ! "$tmp_cloudflared" --version >/dev/null 2>&1; then
        rm -f "$tmp_cloudflared"
        _error "下载的 cloudflared 无法执行，已拒绝安装。"
        return 1
    fi
    mv -f "$tmp_cloudflared" "$CLOUDFLARED_BIN" || { rm -f "$tmp_cloudflared"; return 1; }

    _success "cloudflared 安装成功: $(${CLOUDFLARED_BIN} --version 2>&1 | head -n1)"
}

# --- Argo Tunnel 功能 ---

_prepare_argo_runtime_dir() {
    if [ -L "$RUNTIME_DIR" ]; then
        _error "Argo 运行目录不能是符号链接: $RUNTIME_DIR"
        return 1
    fi
    mkdir -p "$RUNTIME_DIR" || return 1
    chmod 700 "$RUNTIME_DIR" 2>/dev/null || return 1
}

_argo_pid_file() {
    local port="$1"
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || return 1
    printf '%s/argo-%s.pid\n' "$RUNTIME_DIR" "$port"
}

_argo_log_file() {
    local port="$1"
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || return 1
    printf '/var/log/singbox_argo_%s.log\n' "$port"
}

_migrate_legacy_argo_state() {
    local port="$1" pid_file legacy_pid_file legacy_log_file legacy_pid=""
    pid_file=$(_argo_pid_file "$port") || return 1
    _prepare_argo_runtime_dir || return 1
    legacy_pid_file="/tmp/singbox_argo_${port}.pid"
    legacy_log_file="/tmp/singbox_argo_${port}.log"

    if _is_pid_file_running_cmd "$pid_file" "$CLOUDFLARED_BIN"; then
        rm -f -- "$legacy_pid_file" "$legacy_log_file"
        return 0
    fi
    rm -f -- "$pid_file"

    # /tmp 旧 PID 只有在不是符号链接且 cmdline 确认为 cloudflared 时才迁移。
    if [ -s "$legacy_pid_file" ] && [ ! -L "$legacy_pid_file" ]; then
        legacy_pid=$(cat "$legacy_pid_file" 2>/dev/null)
        if _is_pid_running_cmd "$legacy_pid" "$CLOUDFLARED_BIN"; then
            printf '%s\n' "$legacy_pid" > "$pid_file" || return 1
            chmod 600 "$pid_file" 2>/dev/null || { rm -f -- "$pid_file"; return 1; }
        fi
    fi
    rm -f -- "$legacy_pid_file" "$legacy_log_file"
}

_argo_domain_resolves() {
    local domain="$1" remote_ip="" dns_json=""
    [[ "$domain" =~ ^[A-Za-z0-9-]+\.trycloudflare\.com$ ]] || return 1

    if command -v getent >/dev/null 2>&1; then
        getent ahostsv4 "$domain" >/dev/null 2>&1 && return 0
        getent hosts "$domain" >/dev/null 2>&1 && return 0
    fi
    if command -v nslookup >/dev/null 2>&1; then
        nslookup "$domain" >/dev/null 2>&1 && return 0
    fi

    # 系统递归解析器可能缓存刚创建域名的 NXDOMAIN。Quick Tunnel 属于
    # Cloudflare 服务，额外用其 DoH 端点核验公开记录，避免本机负缓存误判。
    if command -v curl >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
        dns_json=$(curl -fsS --connect-timeout 5 --max-time 10 \
            -H 'accept: application/dns-json' --get \
            --data-urlencode "name=${domain}" --data-urlencode 'type=A' \
            'https://1.1.1.1/dns-query' 2>/dev/null || true)
        printf '%s' "$dns_json" | jq -e '
            (.Status == 0) and any(.Answer[]?; (.type == 1 or .type == 28) and ((.data // "") | length > 0))
        ' >/dev/null 2>&1 && return 0
    fi

    # 精简容器可能同时缺少 getent/nslookup。curl 即使收到后端协议错误，
    # 只要已经解析并连接到边缘节点，也会填充 remote_ip。
    remote_ip=$(curl -ksS --connect-timeout 5 --max-time 8 -o /dev/null \
        -w '%{remote_ip}' "https://${domain}/" 2>/dev/null || true)
    [ -n "$remote_ip" ]
}

_start_argo_tunnel() {
    local target_port="$1"
    local protocol="$2"
    local token="$3" # 可选，用于固定隧道
    local retry_count="${4:-0}"

    # 基于端口生成独立的受保护 PID 和日志路径，并迁移旧 /tmp 状态。
    local pid_file log_file
    pid_file=$(_argo_pid_file "$target_port") || { _error "无效的 Argo 内部端口: $target_port"; return 1; }
    log_file=$(_argo_log_file "$target_port") || return 1
    _migrate_legacy_argo_state "$target_port" || return 1

    _info "正在启动 Argo 隧道 (端口: $target_port)..." >&2

    # 检查该端口对应的隧道是否已在运行
    if [ -f "$pid_file" ]; then
        local old_pid
        old_pid=$(cat "$pid_file" 2>/dev/null)
        if _is_pid_running_cmd "$old_pid" "$CLOUDFLARED_BIN"; then
            _warning "检测到端口 $target_port 的 Argo 隧道已在运行 (PID: $old_pid)" >&2
            return 0
        fi
    fi

    # 清理旧日志和同步时间
    rm -f -- "$pid_file" "$log_file"
    : > "$log_file" || return 1
    chmod 600 "$log_file" 2>/dev/null || return 1
    _sync_system_time

    if [ -n "$token" ]; then
        # --- Token 固定隧道模式 ---
        _info "启动固定隧道 (Token 模式)..." >&2

        # 强制锁定 protocol http2 (h2)，防止 QUIC (UDP) 被阻断导致连接失败
        # 增加 --no-autoupdate 防止在精简系统上因自更新导致的意外进程挂起
        nohup "${CLOUDFLARED_BIN}" tunnel --protocol http2 --no-autoupdate run --token "$token" > "${log_file}" 2>&1 &

        local cf_pid=$!
        printf '%s\n' "$cf_pid" > "${pid_file}"
        chmod 600 "$pid_file" 2>/dev/null || true

        sleep 5
        if ! _is_pid_running_cmd "$cf_pid" "$CLOUDFLARED_BIN"; then
             _error "cloudflared 进程已退出！" >&2
             _error "Token 可能无效，或者网络连接被拒绝。" >&2
             echo "--- 错误日志 (最后 20 行) ---" >&2
             cat "${log_file}" | tail -20 >&2
             echo "-----------------------------"
             rm -f -- "$pid_file"
             return 1
        fi
        _enable_argo_watchdog
        _success "Argo 固定隧道 (端口: $target_port) 启动成功!" >&2
        return 0
    else
        # --- URL 临时隧道模式 ---
        _info "启动临时隧道，指向 127.0.0.1:${target_port}..." >&2

        # 优化：强制指定 http2 协议并禁用自动更新
        nohup "${CLOUDFLARED_BIN}" tunnel --protocol http2 --no-autoupdate --url "http://127.0.0.1:${target_port}" \
            --logfile "${log_file}" \
            > /dev/null 2>&1 &

        local cf_pid=$!
        printf '%s\n' "$cf_pid" > "${pid_file}"
        chmod 600 "$pid_file" 2>/dev/null || true

        # 等待隧道启动并获取域名
        _info "等待隧道建立 (最多30秒)..." >&2

        local tunnel_domain=""
        local wait_count=0
        local max_wait=30

        while [ $wait_count -lt $max_wait ]; do
            sleep 2
            wait_count=$((wait_count + 2))

            # 检查进程是否还在运行
            if ! _is_pid_running_cmd "$cf_pid" "$CLOUDFLARED_BIN"; then
                _error "cloudflared 进程已退出，请检查日志: ${log_file}" >&2
                cat "${log_file}" 2>/dev/null | tail -20 >&2
                return 1
            fi

            # 优化域名提取正则表达式，确保无论日志格式如何变化都能准确抓取
            if [ -f "${log_file}" ]; then
                tunnel_domain=$(grep -oE 'https?://[a-zA-Z0-9-]+\.trycloudflare\.com' "${log_file}" 2>/dev/null | head -n 1 | sed -E 's|https?://||')
                if [ -n "$tunnel_domain" ]; then
                    break
                fi
            fi
            echo -n "." >&2
        done
        echo "" >&2

        if [ -n "$tunnel_domain" ]; then
            _info "域名已获取，正在等待 DNS 发布 (最多90秒)..." >&2
            local dns_wait=0 dns_ready=false
            while [ "$dns_wait" -lt 90 ]; do
                if _argo_domain_resolves "$tunnel_domain"; then
                    dns_ready=true
                    break
                fi
                if ! _is_pid_running_cmd "$cf_pid" "$CLOUDFLARED_BIN"; then
                    break
                fi
                sleep 5
                dns_wait=$((dns_wait + 5))
            done

            if [ "$dns_ready" != true ]; then
                _warn "Cloudflare 已分配临时域名，但 DNS 尚未发布，正在清理本次隧道。" >&2
                _is_pid_running_cmd "$cf_pid" "$CLOUDFLARED_BIN" && kill "$cf_pid" 2>/dev/null
                wait "$cf_pid" 2>/dev/null || true
                rm -f -- "$pid_file" "$log_file"
                if [ "$retry_count" -lt 1 ]; then
                    _warn "正在自动重新申请一次临时域名..." >&2
                    _start_argo_tunnel "$target_port" "$protocol" "" "$((retry_count + 1))"
                    return $?
                fi
                _error "Cloudflare 临时域名连续两次未完成 DNS 发布，请稍后重试。" >&2
                return 1
            fi

            _info "域名已发布，正在进行稳定性测试 (5秒)..." >&2
            sleep 5
            if ! _is_pid_running_cmd "$cf_pid" "$CLOUDFLARED_BIN"; then
                 _error "稳定性测试失败：cloudflared 进程异常退出。" >&2
                 cat "${log_file}" 2>/dev/null | tail -n 10 >&2
                 return 1
            fi

            _enable_argo_watchdog
            _success "Argo 临时隧道建立成功: ${tunnel_domain}" >&2
            echo "$tunnel_domain"
            return 0
        else
            _error "获取临时域名超时。请检查网络。日志最后几行：" >&2
            cat "${log_file}" 2>/dev/null | tail -n 5 >&2
            _is_pid_running_cmd "$cf_pid" "$CLOUDFLARED_BIN" && kill "$cf_pid" 2>/dev/null
            rm -f "${pid_file}"
            return 1
        fi
    fi
}

_stop_argo_tunnel() {
    local target_port="$1"
    local pid_file log_file
    pid_file=$(_argo_pid_file "$target_port") || return 1
    log_file=$(_argo_log_file "$target_port") || return 1
    _migrate_legacy_argo_state "$target_port" || return 1

    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null)
        if _is_pid_running_cmd "$pid" "$CLOUDFLARED_BIN"; then
            kill "$pid" 2>/dev/null
            _success "Argo 隧道 (端口: $target_port) 已停止"
        fi
    fi
    rm -f -- "$pid_file" "$log_file" "/tmp/singbox_argo_${target_port}.pid" "/tmp/singbox_argo_${target_port}.log"
}


# ============================================================
# 统一 Argo 节点创建函数 (消除 VLESS/Trojan 重复代码)
# 参数: $1 = 协议类型 ("vless" 或 "trojan")
# ============================================================
_add_argo_node() {
    local protocol="$1"
    local protocol_label=""
    local proto_name=""
    case "$protocol" in
        vless) protocol_label="VLESS-WS"; proto_name="Vless" ;;
        trojan) protocol_label="Trojan-WS"; proto_name="Trojan" ;;
        *) _error "不支持的 Argo 协议: $protocol"; return 1 ;;
    esac

    _info "--- 创建 ${protocol_label} + Argo 隧道节点 ---"

    # 安装 cloudflared
    _install_cloudflared || return 1

    # === [公共] 内部端口分配 ===
    read -p "请输入 Argo 内部监听端口 (回车随机生成): " input_port
    local port="$input_port"

    while true; do
        if [[ -n "$port" && "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ]; then
            _check_port_conflict "$port" "tcp" && port="" && continue
            _info "已使用监听端口: ${port}"
            break
        else
            [ -n "$port" ] && _warning "端口格式无效，将重新生成..."
            # 使用内建算法生成随机端口 (10000-60000)，移除 shuf 依赖
            port=$(( $(od -An -tu2 -N2 /dev/urandom | tr -d ' ') % 50001 + 10000 ))
            _info "正在尝试分配随机内部端口: ${port}..."
        fi
    done

    # === [公共] WebSocket 路径 ===
    read -p "请输入 WebSocket 路径 (回车随机生成): " ws_path
    if [ -z "$ws_path" ]; then
        ws_path="/"$(${SINGBOX_BIN} generate rand --hex 8)
        _info "已生成随机路径: ${ws_path}"
    else
        [[ ! "$ws_path" == /* ]] && ws_path="/${ws_path}"
    fi

    # === [协议特定] Trojan 密码输入 ===
    local password=""
    if [ "$protocol" == "trojan" ]; then
        read -p "请输入 Trojan 密码 (回车随机生成): " password
        if [ -z "$password" ]; then
            password=$(${SINGBOX_BIN} generate rand --hex 16)
            _info "已生成随机密码: ${password}"
        fi
    fi

    # === [公共] 隧道模式选择 ===
    echo ""
    echo "请选择隧道模式:"
    echo "  1. 临时隧道 (无需配置, 随机域名, 不稳定，重启失效)"
    echo "  2. 固定隧道 (需 Token, 自定义域名, 稳定持久，重启不失效)"
    read -p "请选择 [1/2] (默认: 1): " tunnel_mode
    tunnel_mode=${tunnel_mode:-1}

    local token=""
    local tunnel_domain=""
    local argo_type="temp"

    if [ "$tunnel_mode" == "2" ]; then
        argo_type="fixed"
        _info "您选择了 [固定隧道] 模式。"
        echo ""
        _info "请粘贴 Cloudflare Tunnel Token (支持直接粘贴CF网页端所给出的任何安装命令):"
        read -p "Token: " input_token
        # 自动提取 Token
        token=$(echo "$input_token" | grep -oE 'ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+' | head -1)
        if [ -z "$token" ]; then
             token=$(echo "$input_token" | grep -oE 'ey[A-Za-z0-9_-]{20,}' | head -1)
        fi
        if [ -z "$token" ]; then
             token="$input_token"
        fi

        if [ -z "$token" ]; then _error "Token 不能为空"; return 1; fi
        _info "已识别 Token (前20位): ${token:0:20}..."

        echo ""
        _info "请输入该 Tunnel 绑定的域名 (用于生成客户端配置):"
        read -p "域名 (例如 tunnel.example.com): " input_domain
        if [ -z "$input_domain" ]; then _error "域名不能为空"; return 1; fi
        tunnel_domain="$input_domain"

        echo ""
        _info "【重要提示】请务必去 Cloudflare Dashboard 配置该 Tunnel 的 Public Hostname:"
        _info "  Public Hostname: ${tunnel_domain}"
        _info "  Service: http://localhost:${port}"
        echo ""
        read -n 1 -s -r -p "确认配置无误后，按任意键继续..."
        echo ""
    else
        _info "您选择了 [临时隧道] 模式。"
    fi

    # === [公共] 节点名称 ===
    local default_prefix="Argo-Temp"
    if [ "$argo_type" == "fixed" ]; then
        default_prefix="Argo-Fixed"
    fi
    local default_name="${default_prefix}-${proto_name}-${port}"

    echo ""
    read -p "请输入节点名称 (默认: ${default_name}): " custom_name
    local name=${custom_name:-$default_name}

    # === [协议特定] 生成凭据、tag 和 Inbound ===
    local tag="argo-${protocol}-ws-${port}"
    local uuid=""
    local inbound_json=""

    if [ "$protocol" == "vless" ]; then
        uuid=$(${SINGBOX_BIN} generate uuid)
        inbound_json=$(jq -n \
            --arg t "$tag" \
            --arg p "$port" \
            --arg u "$uuid" \
            --arg wsp "$ws_path" \
            --argjson ed "$WS_EARLY_DATA_SIZE" \
            --arg edh "$WS_EARLY_DATA_HEADER" \
            '{
                "type": "vless",
                "tag": $t,
                "listen": "127.0.0.1",
                "listen_port": ($p|tonumber),
                "users": [{"uuid": $u, "flow": ""}],
                "transport": {
                    "type": "ws",
                    "path": $wsp,
                    "max_early_data": $ed,
                    "early_data_header_name": $edh
                }
            }')
    elif [ "$protocol" == "trojan" ]; then
        inbound_json=$(jq -n \
            --arg t "$tag" \
            --arg p "$port" \
            --arg pw "$password" \
            --arg wsp "$ws_path" \
            --argjson ed "$WS_EARLY_DATA_SIZE" \
            --arg edh "$WS_EARLY_DATA_HEADER" \
            '{
                "type": "trojan",
                "tag": $t,
                "listen": "127.0.0.1",
                "listen_port": ($p|tonumber),
                "users": [{"password": $pw}],
                "transport": {
                    "type": "ws",
                    "path": $wsp,
                    "max_early_data": $ed,
                    "early_data_header_name": $edh
                }
            }')
    fi

    _atomic_modify_json "$CONFIG_FILE" ".inbounds += [$inbound_json]" || return 1

    # === [公共] 重启 + 启动隧道 ===
    _manage_service "restart"
    sleep 2

    if [ "$argo_type" == "fixed" ]; then
        if ! _start_argo_tunnel "$port" "${protocol}-ws" "$token"; then
             _atomic_modify_json "$CONFIG_FILE" "del(.inbounds[] | select(.tag == \"$tag\"))"
             _manage_service "restart"
             return 1
        fi
    else
        local real_domain=$(_start_argo_tunnel "$port" "${protocol}-ws")
        if [ -z "$real_domain" ] || [ "$real_domain" == "" ]; then
            _error "隧道启动失败，正在回滚配置..."
            _atomic_modify_json "$CONFIG_FILE" "del(.inbounds[] | select(.tag == \"$tag\"))"
            _manage_service "restart"
            return 1
        fi
        tunnel_domain="$real_domain"
    fi

    # === [协议特定] 保存元数据 ===
    local credential_key="" credential_val=""
    if [ "$protocol" == "vless" ]; then
        credential_key="uuid"; credential_val="$uuid"
    else
        credential_key="password"; credential_val="$password"
    fi

    local argo_meta=$(jq -n \
        --arg tag "$tag" \
        --arg name "$name" \
        --arg domain "$tunnel_domain" \
        --arg port "$port" \
        --arg cred_val "$credential_val" \
        --arg cred_key "$credential_key" \
        --arg path "$ws_path" \
        --arg protocol "${protocol}-ws" \
        --arg type "$argo_type" \
        --arg token "$token" \
        --arg created "$(date '+%Y-%m-%d %H:%M:%S')" \
        '{($tag): {name: $name, domain: $domain, local_port: ($port|tonumber), ($cred_key): $cred_val, path: $path, protocol: $protocol, type: $type, token: $token, created_at: $created}}')

    if [ ! -f "$ARGO_METADATA_FILE" ]; then
        echo '{}' > "$ARGO_METADATA_FILE"
    fi
    _atomic_modify_json "$ARGO_METADATA_FILE" ". + $argo_meta"

    # === [协议特定] Clash 配置 + 分享链接 ===
    local proxy_json=""
    if [ "$protocol" == "vless" ]; then
        proxy_json=$(jq -n \
            --arg n "$name" \
            --arg s "$tunnel_domain" \
            --arg u "$uuid" \
            --arg wsp "$ws_path" \
            --argjson ed "$WS_EARLY_DATA_SIZE" \
            --arg edh "$WS_EARLY_DATA_HEADER" \
            '{
                "name": $n,
                "type": "vless",
                "server": $s,
                "port": 443,
                "uuid": $u,
                "tls": true,
                "udp": true,
                "skip-cert-verify": false,
                "network": "ws",
                "servername": $s,
                "ws-opts": {
                    "path": $wsp,
                    "max-early-data": $ed,
                    "early-data-header-name": $edh,
                    "headers": {
                        "Host": $s
                    }
                }
            }')
    elif [ "$protocol" == "trojan" ]; then
        proxy_json=$(jq -n \
            --arg n "$name" \
            --arg s "$tunnel_domain" \
            --arg pw "$password" \
            --arg wsp "$ws_path" \
            --argjson ed "$WS_EARLY_DATA_SIZE" \
            --arg edh "$WS_EARLY_DATA_HEADER" \
            '{
                "name": $n,
                "type": "trojan",
                "server": $s,
                "port": 443,
                "password": $pw,
                "udp": true,
                "skip-cert-verify": false,
                "network": "ws",
                "sni": $s,
                "ws-opts": {
                    "path": $wsp,
                    "max-early-data": $ed,
                    "early-data-header-name": $edh,
                    "headers": {
                        "Host": $s
                    }
                }
            }')
    fi

    if ! _add_node_to_yaml "$proxy_json"; then
        _stop_argo_tunnel "$port"
        _atomic_modify_json "$CONFIG_FILE" 'del(.inbounds[] | select(.tag == $tag))' --arg tag "$tag" >/dev/null 2>&1 || true
        _atomic_modify_json "$ARGO_METADATA_FILE" 'del(.[$tag])' --arg tag "$tag" >/dev/null 2>&1 || true
        return 1
    fi

    # === [公共] 启用守护 + 显示结果 ===
    # 守护是 Argo 节点的完整性条件；失败时回滚本次节点，避免产生
    # 表面创建成功但重启后无法自愈的半成品状态。
    if ! _enable_argo_watchdog; then
        _stop_argo_tunnel "$port"
        _atomic_modify_json "$CONFIG_FILE" 'del(.inbounds[] | select(.tag == $tag))' --arg tag "$tag" >/dev/null 2>&1 || true
        _atomic_modify_json "$ARGO_METADATA_FILE" 'del(.[$tag])' --arg tag "$tag" >/dev/null 2>&1 || true
        _remove_node_from_yaml "$name" >/dev/null 2>&1 || true
        _manage_service "restart" >/dev/null 2>&1 || true
        return 1
    fi

    echo ""
    _success "${protocol_label} + Argo 节点创建成功!"
    echo "-------------------------------------------"
    echo -e "节点名称: ${GREEN}${name}${NC}"
    echo -e "隧道类型: ${CYAN}${argo_type}${NC}"
    echo -e "隧道域名: ${CYAN}${tunnel_domain}${NC}"
    echo -e "本地端口: ${port}"
    echo "-------------------------------------------"

    # 使用统一链接生成器进行展示与持久化
    if [ "$protocol" == "vless" ]; then
        _show_node_link "vless-ws" "$name" "$tunnel_domain" "443" "$tag" "$uuid" "$ws_path"
    else
        _show_node_link "trojan-ws" "$name" "$tunnel_domain" "443" "$tag" "$password" "$ws_path"
    fi

    echo "-------------------------------------------"
    if [ "$argo_type" == "temp" ]; then
        _warning "注意: 临时隧道每次重启域名会变化！"
    fi
}

# 保留原始函数名作为薄包装器，确保向后兼容
_add_argo_vless_ws() { _add_argo_node "vless"; }




# --- Argo 守护进程逻辑 ---

_argo_keepalive() {
    # keepalive 使用受保护运行目录中的 flock，避免可预测 /tmp 锁和并发重启。
    _prepare_argo_runtime_dir || return 1
    command -v flock >/dev/null 2>&1 || { _error "缺少 flock，Argo 守护任务已拒绝无锁运行。"; return 1; }
    exec 7>"${RUNTIME_DIR}/argo-keepalive.lock" || return 1
    flock -n 7 || { exec 7>&-; return 0; }

    # --- 性能优化: 日志轮转 (10MB) ---
    local max_size=$((10 * 1024 * 1024))
    local log_size
    for log in "$LOG_FILE" "$ARGO_LOG_FILE"; do
        log_size=0
        [ -f "$log" ] && log_size=$(wc -c < "$log" 2>/dev/null || echo 0)
        if [[ "$log_size" =~ ^[0-9]+$ ]] && [ "$log_size" -ge "$max_size" ]; then
            local log_tmp
            log_tmp=$(mktemp "${log}.tmp.XXXXXX") || continue
            if tail -n 1000 "$log" > "$log_tmp"; then
                chmod 600 "$log_tmp" 2>/dev/null || true
                mv -f "$log_tmp" "$log"
            else
                rm -f "$log_tmp"
            fi
        fi
    done

    # 如果元数据文件不存在或为空，不需要守护
    if [ ! -f "$ARGO_METADATA_FILE" ] || [ "$(jq 'length' "$ARGO_METADATA_FILE" 2>/dev/null)" -eq 0 ]; then
        flock -u 7 2>/dev/null || true
        exec 7>&-
        return
    fi

    # 遍历所有节点。这里不能使用 TSV 配合 IFS=$'\t'：临时隧道的 token
    # 必然为空，而 Bash 会折叠连续的空白 IFS 字符，导致 protocol/name/uuid
    # 整体左移，守护重启后甚至会把 UUID 误写成节点名称。逐条读取紧凑 JSON
    # 可同时正确保留空字段以及名称、路径中的空格和分隔符。
    local entry tag port type token protocol name uuid password path
    while IFS= read -r entry; do
        [ -z "$entry" ] && continue
        tag=$(jq -r '.key // empty' <<< "$entry")
        port=$(jq -r '.value.local_port // empty' <<< "$entry")
        type=$(jq -r '.value.type // ""' <<< "$entry")
        token=$(jq -r '.value.token // ""' <<< "$entry")
        protocol=$(jq -r '.value.protocol // "vless-ws"' <<< "$entry")
        name=$(jq -r '.value.name // ""' <<< "$entry")
        uuid=$(jq -r '.value.uuid // ""' <<< "$entry")
        password=$(jq -r '.value.password // ""' <<< "$entry")
        path=$(jq -r '.value.path // ""' <<< "$entry")
        [ -z "$tag" ] && continue
        [[ "$port" =~ ^[0-9]+$ ]] || {
            logger "sing-box-watchdog: Ignoring invalid Argo metadata entry: $tag"
            continue
        }

        local pid_file
        _migrate_legacy_argo_state "$port" >/dev/null 2>&1 || true
        pid_file=$(_argo_pid_file "$port")
        local is_running=false

        if [ -f "$pid_file" ]; then
            local pid=$(cat "$pid_file" 2>/dev/null)
            if _is_pid_running_cmd "$pid" "$CLOUDFLARED_BIN"; then
                is_running=true
            fi
        fi

        if [ "$is_running" = false ]; then
            logger "sing-box-watchdog: Detected dead tunnel for $tag (Port: $port). Restarting..."

            # 提取 protocol 简写用于 _start_argo_tunnel (vless/trojan)
            local proto_short="vless"
            [[ "$protocol" == "trojan-ws" ]] && proto_short="trojan"

            if [ "$type" == "fixed" ] && [ -n "$token" ]; then
                 if _start_argo_tunnel "$port" "$proto_short-ws" "$token"; then
                     logger "sing-box-watchdog: Fixed tunnel $tag restarted successfully."
                 else
                     logger "sing-box-watchdog: Failed to restart fixed tunnel $tag."
                 fi
            else
                 # 临时隧道
                 local new_domain=$(_start_argo_tunnel "$port" "$proto_short-ws")
                 if [ -n "$new_domain" ]; then
                      # 更新元数据
                      _atomic_modify_json "$ARGO_METADATA_FILE" '.[$tag].domain = $domain' \
                          --arg tag "$tag" --arg domain "$new_domain"
                      logger "sing-box-watchdog: Temp tunnel $tag restarted with new domain: $new_domain"

                      # [同步链接] 临时域名变动，静默更新持久化链接
                      if [[ "$protocol" == "vless-ws" ]]; then
                          _show_node_link "vless-ws" "$name" "$new_domain" "443" "$tag" "$uuid" "$path" >/dev/null
                      else
                          _show_node_link "trojan-ws" "$name" "$new_domain" "443" "$tag" "$password" "$path" >/dev/null
                      fi
                 else
                      logger "sing-box-watchdog: Failed to restart temp tunnel $tag."
                 fi
            fi
        fi
    done < <(jq -c 'to_entries[]' "$ARGO_METADATA_FILE" 2>/dev/null)
    flock -u 7 2>/dev/null || true
    exec 7>&-
}

_ensure_argo_cron() {
    local cron_pkg="cron"
    local services=(cron crond cronie)
    local service_name started=false

    if command -v apk &>/dev/null; then
        cron_pkg="dcron"
        services=(dcron crond)
    elif ! command -v apt-get &>/dev/null; then
        cron_pkg="cronie"
        services=(crond cronie)
    fi

    if ! command -v crontab &>/dev/null; then
        _info "正在按需安装 Argo 守护依赖: ${cron_pkg}..."
        if ! _pkg_install "$cron_pkg" || ! command -v crontab &>/dev/null; then
            _error "安装 Argo 守护依赖失败，未创建不受守护的隧道节点。"
            return 1
        fi
    fi

    case "${INIT_SYSTEM:-}" in
        systemd)
            for service_name in "${services[@]}"; do
                if systemctl enable --now "${service_name}.service" >/dev/null 2>&1; then
                    started=true
                    break
                fi
            done
            ;;
        openrc)
            for service_name in "${services[@]}"; do
                if rc-service "$service_name" start >/dev/null 2>&1; then
                    rc-update add "$service_name" default >/dev/null 2>&1 || true
                    started=true
                    break
                fi
            done
            ;;
        *)
            if command -v service &>/dev/null; then
                for service_name in "${services[@]}"; do
                    if service "$service_name" start >/dev/null 2>&1; then
                        started=true
                        break
                    fi
                done
            fi
            ;;
    esac

    if [ "$started" != true ]; then
        _error "Cron 服务启动失败，无法启用 Argo 自动守护。"
        return 1
    fi
}

_enable_argo_watchdog() {
    # Podman 低内存安装会跳过可选包；仅在使用 Argo 时按需安装 cron。
    local job="* * * * * bash ${SELF_SCRIPT_PATH} keepalive >/dev/null 2>&1"

    _ensure_argo_cron || return 1
    if crontab -l 2>/dev/null | grep -Fq "$job"; then
        return 0
    fi

    _info "正在添加后台守护进程 (Watchdog)..."
    if (crontab -l 2>/dev/null; echo "$job") | crontab -; then
        _success "守护进程已启用！(每分钟检查并自动修复失效隧道)"
        return 0
    fi

    _error "添加 Crontab 失败，未创建不受守护的隧道节点。"
    return 1
}

_disable_argo_watchdog() {
    local job="bash ${SELF_SCRIPT_PATH} keepalive"

    if crontab -l 2>/dev/null | grep -Fq "$job"; then
        _info "正在移除后台守护进程..."
        crontab -l 2>/dev/null | grep -Fv "$job" | crontab -
        _success "守护进程已移除。"
    fi
}


# --- 服务与配置管理 ---

_create_systemd_service() {
    local mem_limit_mb=$(_get_mem_limit)

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=sing-box service
Documentation=https://sing-box.sagernet.org
After=network.target nss-lookup.target

[Service]
Environment="GOMEMLIMIT=${mem_limit_mb}MiB"
ExecStart=${SINGBOX_BIN} run -c ${CONFIG_FILE} -c ${SINGBOX_DIR}/relay.json
Restart=on-failure
RestartSec=3s
LimitNOFILE=infinity

[Install]
WantedBy=multi-user.target
EOF
}

_create_openrc_service() {
    # 确保日志文件存在
    touch "${LOG_FILE}"
    local mem_limit_mb=$(_get_mem_limit)

    cat > "$SERVICE_FILE" <<EOF
#!/sbin/openrc-run

description="sing-box service"
export GOMEMLIMIT="${mem_limit_mb}MiB"
command="${SINGBOX_BIN}"
command_args="run -c ${CONFIG_FILE} -c ${SINGBOX_DIR}/relay.json"
# 使用 supervise-daemon 实现守护和重启
supervisor="supervise-daemon"
respawn_delay=3
respawn_max=0

pidfile="${PID_FILE}"
# supervise-daemon 自动将 stdout/stderr 重定向功能需要 openrc 版本支持
# 如果不支持，日志可能不会输出到文件，但服务能正常运行
output_log="${LOG_FILE}"
error_log="${LOG_FILE}"

start_pre() {
    checkpath -d -m 0700 ${RUNTIME_DIR}
}

depend() {
    need net
    after firewall
}
EOF
    chmod +x "$SERVICE_FILE"
}

_create_service_files() {

    _info "正在创建 ${INIT_SYSTEM} 服务文件..."
    if [ "$INIT_SYSTEM" == "systemd" ]; then
        _create_systemd_service
        systemctl daemon-reload
        systemctl enable sing-box
    elif [ "$INIT_SYSTEM" == "openrc" ]; then
        touch "$LOG_FILE"
        _create_openrc_service
        rc-update add sing-box default
    elif [ "$INIT_SYSTEM" == "direct" ]; then
        touch "$LOG_FILE"
        _info "当前容器没有 systemd/openrc，将使用 direct 后台模式管理 sing-box。"
        return 0
    fi
    _success "${INIT_SYSTEM} 服务创建并启用成功。"
}

# 每 48 小时清空一次本脚本产生的运行日志，避免小磁盘被持续写满。
_cleanup_runtime_logs() {
    local state_file="${SINGBOX_DIR}/.last_log_cleanup"
    local now last
    now=$(date +%s 2>/dev/null) || return 1
    last=$(cat "$state_file" 2>/dev/null)

    # 首次启用时立即清理一次，先释放可能已经被占满的磁盘空间。
    if ! [[ "$last" =~ ^[0-9]+$ ]]; then
        last=0
    fi
    [ $((now - last)) -lt 172800 ] && return 0

    local log
    for log in "$LOG_FILE" "$ARGO_LOG_FILE" /var/log/xray.log /var/log/singbox_argo_*.log; do
        [ -f "$log" ] && : > "$log"
    done
    printf '%s\n' "$now" > "$state_file"
}

_setup_log_cleanup() {
    command -v crontab >/dev/null 2>&1 || {
        _warning "未找到 crontab，无法启用每 2 天自动清理日志。"
        return 1
    }

    local tag="# sing-box-log-cleanup"
    local job="17 * * * * bash ${SELF_SCRIPT_PATH} cleanup-logs >/dev/null 2>&1 ${tag}"
    local current
    current=$(crontab -l 2>/dev/null || true)
    if ! printf '%s\n' "$current" | grep -Fq "$tag"; then
        { printf '%s\n' "$current"; printf '%s\n' "$job"; } | sed '/^$/d' | crontab - || return 1
    fi
    _cleanup_runtime_logs
}


# 注意: _manage_service 已在上方定义，此处不再重复定义

_initialize_config_files() {
    mkdir -p ${SINGBOX_DIR}
    if [ ! -s "$CONFIG_FILE" ]; then
        # 初始化包含完整 dns 配置和路由策略的基础文件，以支持中转第三方域名节点
        cat > "$CONFIG_FILE" << 'EOF'
{
  "ntp": {
    "enabled": true,
    "server": "time.apple.com",
    "server_port": 123,
    "interval": "1m",
    "write_to_system": false,
    "connect_timeout": "3s"
  },
  "dns": {
    "servers": [
      {
        "type": "local",
        "tag": "dns-local",
        "prefer_go": true
      }
    ],
    "final": "dns-local",
    "strategy": "prefer_ipv4"
  },
  "inbounds": [],
  "outbounds": [
    {
      "type": "direct",
      "tag": "direct"
    }
  ],
  "route": {
    "rules": [],
    "final": "direct",
    "default_domain_resolver": {
      "server": "dns-local",
      "strategy": "prefer_ipv4"
    }
  }
}
EOF
    fi
    [ -s "$METADATA_FILE" ] || echo "{}" > "$METADATA_FILE"

    # [关键修复] 初始化 relay.json - 服务启动命令会加载这个文件
    # 必须确保在服务运行前此文件物理存在，否则 sing-box 会 Fatal 退出
    local RELAY_JSON="${SINGBOX_DIR}/relay.json"
    if [ ! -s "$RELAY_JSON" ]; then
        echo '{"inbounds":[],"outbounds":[],"route":{"rules":[]}}' > "$RELAY_JSON"
        _info "已初始化中转配置文件: $RELAY_JSON"
    fi
    if [ ! -s "$CLASH_YAML_FILE" ]; then
        _info "正在创建全新的 clash.yaml 配置文件..."
        cat > "$CLASH_YAML_FILE" << 'EOF'
port: 7890
socks-port: 7891
mixed-port: 7892
allow-lan: false
bind-address: '*'
mode: rule
log-level: info
ipv6: true
find-process-mode: strict
external-controller: '127.0.0.1:9090'
profile:
  store-selected: true
  store-fake-ip: true
unified-delay: true
tcp-concurrent: true
ntp:
  enable: true
  write-to-system: false
  server: ntp.aliyun.com
  port: 123
  interval: 30
dns:
  enable: true
  respect-rules: true
  use-system-hosts: true
  prefer-h3: false
  listen: '0.0.0.0:1053'
  ipv6: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  use-hosts: true
  fake-ip-filter:
    - +.lan
    - +.local
    - localhost.ptlogin2.qq.com
    - +.msftconnecttest.com
    - +.msftncsi.com
  nameserver:
    - 1.1.1.1
    - 8.8.8.8
    - 'https://1.1.1.1/dns-query'
    - 'https://dns.quad9.net/dns-query'
  default-nameserver:
    - 1.1.1.1
    - 8.8.8.8
  proxy-server-nameserver:
    - 223.5.5.5
    - 119.29.29.29
  fallback:
    - 'https://1.0.0.1/dns-query'
    - 'https://9.9.9.10/dns-query'
  fallback-filter:
    geoip: true
    geoip-code: CN
    ipcidr:
      - 240.0.0.0/4
tun:
  enable: true
  stack: system
  auto-route: true
  auto-detect-interface: true
  strict-route: false
  dns-hijack:
    - 'any:53'
  device: SakuraiTunnel
  endpoint-independent-nat: true
proxies: []
proxy-groups:
  - name: 节点选择
    type: select
    proxies: []
rules:
  - GEOIP,PRIVATE,DIRECT,no-resolve
  - GEOIP,CN,DIRECT
  - MATCH,节点选择
EOF
    fi
}

_init_relay_config() {
    # 确保中转配置文件存在 (隔离配置)
    if [ ! -s "$RELAY_CONFIG_FILE" ]; then
        _ensure_relay_config || return 1
        _info "已初始化中转配置文件"
    fi
}

_generate_self_signed_cert() {
    local domain="$1"
    local cert_path="$2"
    local key_path="$3"

    _info "正在为 ${domain} 生成支持 SAN 的高级自签名证书..."

    # 创建临时配置文件用于生成 SAN
    local openssl_config=$(mktemp)
    cat > "$openssl_config" <<EOF
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no
[req_distinguished_name]
CN = ${domain}
[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = ${domain}
DNS.2 = *.${domain}
EOF

    # 使用 RSA 2048 生成证书 (CF 回源兼容性更佳)
    openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
        -keyout "$key_path" -out "$cert_path" \
        -config "$openssl_config" >/dev/null 2>&1

    local status=$?
    rm -f "$openssl_config"

    if [ $status -ne 0 ]; then
        _error "为 ${domain} 生成证书失败！"
        rm -f "$cert_path" "$key_path"
        return 1
    fi
    _success "证书 ${cert_path} (含 SAN) 已成功生成。"
    return 0
}

# 注意: _atomic_modify_json, _atomic_modify_yaml, _get_proxy_field, _add_node_to_yaml, _remove_node_from_yaml
# 均在上方统一定义，此处不再重复定义以避免不一致

# 显示节点分享链接（在添加节点后调用）
# 参数: $1=协议类型, $2=节点名称, $3=服务器IP(用于链接), $4=端口, $5=节点TAG, 其他参数根据协议不同
_show_node_link() { :; }
# 以服务器配置和规范化元数据为唯一事实源，完整重建客户端 YAML 与分享链接。

# --- 更新管理脚本 ---

# 守卫函数：检查 sing-box 核心是否已安装

# [安装/更新 Sing-box 核心] — 最新稳定版或永久固定 v1.13.21

# 执行 sing-box 核心的安装/更新
_do_update_singbox() {
    local install_target="${1:-latest}"
    if [ "$install_target" = "latest" ] && _singbox_core_is_locked; then
        _error "固定版锁已启用，拒绝执行 sing-box 核心升级。"
        return 1
    fi
    case "$install_target" in
        latest|"$SINGBOX_FIXED_VERSION") ;;
        *) _error "无效的 sing-box 安装目标: ${install_target}"; return 1 ;;
    esac

    _info "--- 安装/更新 Sing-box 核心 ---"
    # 更新核心时复用已验证的依赖缓存，避免在 128M 容器中再次触发 apk
    # 的高峰内存占用；缓存缺项时 _install_dependencies 仍会自愈安装。
    _install_dependencies
    if ! _install_sing_box "$install_target"; then
        _error "Sing-box 核心安装/更新失败。"
        return 1
    fi

    _success "sing-box 核心已暂存，正在执行配置和服务验证。"
    if [ ! -f "${CONFIG_FILE}" ] || [ ! -f "${CLASH_YAML_FILE}" ]; then
        _info "检测到主配置文件缺失，正在初始化..."
        _initialize_config_files || { _rollback_singbox_binary; return 1; }
    fi
    _init_relay_config
    _ensure_relay_config || { _rollback_singbox_binary; return 1; }
    _secure_state_permissions

    local check_result
    if ! check_result=$(_check_combined_config_files "$SINGBOX_BIN" "$CONFIG_FILE" "$RELAY_CONFIG_FILE" 2>&1); then
        _error "新核心未通过 config.json + relay.json 组合校验，正在恢复旧核心："
        echo "$check_result"
        _rollback_singbox_binary
        return 1
    fi

    _create_service_files
    _setup_log_cleanup
    _info "正在启动/重启 [主] 服务 (sing-box)..."
    if ! _manage_service restart; then
        _error "新核心启动失败，正在恢复旧核心。"
        _rollback_singbox_binary
        if [ -x "$SINGBOX_BIN" ]; then _manage_service restart >/dev/null 2>&1 || true; fi
        return 1
    fi

    if [ "$install_target" = "$SINGBOX_FIXED_VERSION" ]; then
        if ! _write_singbox_core_lock; then
            _error "固定版本锁写入失败，正在恢复旧核心。"
            _manage_service stop >/dev/null 2>&1 || true
            _rollback_singbox_binary
            if [ -x "$SINGBOX_BIN" ]; then _manage_service restart >/dev/null 2>&1 || true; fi
            return 1
        fi
        _secure_state_permissions
    fi
    _commit_singbox_binary
    if [ "$install_target" = "$SINGBOX_FIXED_VERSION" ]; then
        _success "[主] 服务已使用固定核心 v${SINGBOX_FIXED_VERSION} 启动，版本升级锁已生效。"
    else
        _success "[主] 服务已使用最新稳定核心 v${SINGBOX_STAGED_VERSION:-未知} 启动，更新已提交。"
    fi
}

# [安装/更新 Xray 核心] — 双模态：未装就装、已装就更新

# 执行 Xray 核心的安装/更新 (内联实现，避免依赖 xray_manager.sh 的 source)

# 从主脚本创建 Xray 服务文件 (内联实现)

# --- 进阶功能 (子脚本) ---

# --- Xray 节点管理 (子脚本) ---

# 主节点创建必须作为一个整体提交：协议生成器会依次修改 JSON、YAML、
# 凭据和 nftables；任一步失败都恢复创建前状态，且只在组合校验通过后重启一次。

# 批量创建节点 (v11.3 深度向导版)

# --- 脚本入口 ---


# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        keepalive)
            _argo_keepalive
            exit 0
            ;;
        cleanup-logs)
            _cleanup_runtime_logs
            exit $?
            ;;
        *)
            shift
            ;;
    esac
done

# Panel-owned maintenance entry point; installation uses validated panel jobs.
main() {
 _detect_init_system
 case "${ARGO_ACTION:-}" in
  argo-keepalive) _argo_keepalive;;
  cleanup-logs) _cleanup_runtime_logs;;
  *) echo '请通过面板安装、编辑或卸载 Argo / Reality / Hysteria2 节点。';;
 esac
}
main
