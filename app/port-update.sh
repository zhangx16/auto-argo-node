# Called with validated PORT / OLD_PORT / EXPECTED_UUID and upstream functions loaded.
update_port() { _with_state_lock _update_port_locked; }
_update_port_locked() {
    local old_tag="argo-vless-ws-${OLD_PORT}" new_tag="argo-vless-ws-${PORT}"
    local backup="$WORK/port-backup" committed=0 new_domain=""
    [ -n "$EXPECTED_UUID" ] || { echo 'ERROR: 缺少原节点 UUID，请先读取最新链接'; return 1; }
    jq -e --arg t "$old_tag" --arg u "$EXPECTED_UUID" '.[$t] | select(.uuid == $u and .protocol == "vless-ws" and .type == "temp")' "$ARGO_METADATA_FILE" >/dev/null || {
        echo 'ERROR: 原节点与面板记录不一致，请先读取最新链接'; return 1;
    }
    jq -e --arg t "$old_tag" '.inbounds[] | select(.tag == $t)' "$CONFIG_FILE" >/dev/null || {
        echo 'ERROR: 找不到原节点监听配置'; return 1;
    }
    if [ "$OLD_PORT" = "$PORT" ]; then
        printf 'SB_WEB_RESULT='
        jq -c --arg t "$old_tag" '.[$t]' "$ARGO_METADATA_FILE"
        return
    fi
    if _check_port_conflict "$PORT" tcp; then echo 'ERROR: 新端口被占用或与现有配置冲突'; return 1; fi
    if jq -e --arg t "$new_tag" 'has($t)' "$ARGO_METADATA_FILE" >/dev/null; then
        echo 'ERROR: 新端口已有 Argo 节点'; return 1
    fi
    mkdir -p "$backup" || return 1
    cp "$CONFIG_FILE" "$backup/config" && cp "$ARGO_METADATA_FILE" "$backup/meta" && cp "$CLASH_YAML_FILE" "$backup/yaml" || return 1
    rollback_port() {
        echo '端口修改未完成，正在恢复原配置和原隧道'
        _stop_argo_tunnel "$PORT" >/dev/null 2>&1 || true
        cp "$backup/config" "$CONFIG_FILE" && cp "$backup/meta" "$ARGO_METADATA_FILE" && cp "$backup/yaml" "$CLASH_YAML_FILE" || { echo 'ERROR: 配置恢复失败，请检查服务器'; return 1; }
        _manage_service restart || { echo 'ERROR: 原配置已恢复，但服务重启失败，请检查服务器'; return 1; }
    }
    trap 'if [ "$committed" = 0 ]; then rollback_port; fi' EXIT
    trap 'exit 1' HUP INT TERM
    migrate_port() {
        echo "正在将监听端口 ${OLD_PORT} 修改为 ${PORT}；新隧道就绪前保留原隧道"
        _atomic_modify_json "$CONFIG_FILE" '(.inbounds[] | select(.tag == $old) | .tag = $new | .listen_port = ($p|tonumber)) as $in | .inbounds += [$in]' --arg old "$old_tag" --arg new "$new_tag" --arg p "$PORT" || return 1
        _manage_service restart || return 1
        new_domain=$(trap - EXIT HUP INT TERM; _start_argo_tunnel "$PORT" vless-ws 9>&-) || return 1
        [[ "$new_domain" =~ ^[a-z0-9-]+\.trycloudflare\.com$ ]] || { echo 'ERROR: 未获取有效的新隧道域名'; return 1; }
        _atomic_modify_json "$ARGO_METADATA_FILE" '.[$new] = (.[$old] | .local_port = ($p|tonumber) | .domain = $d) | del(.[$old])' --arg old "$old_tag" --arg new "$new_tag" --arg p "$PORT" --arg d "$new_domain" || return 1
        export NODE_NAME NODE_DOMAIN="$new_domain"
        NODE_NAME=$(jq -r --arg t "$old_tag" '.[$t].name' "$backup/meta") || return 1
        _atomic_modify_yaml "$CLASH_YAML_FILE" '(.proxies[] | select(.name == env(NODE_NAME))) |= (.server = env(NODE_DOMAIN) | .servername = env(NODE_DOMAIN) | .["ws-opts"].headers.Host = env(NODE_DOMAIN))' || return 1
        _atomic_modify_json "$CONFIG_FILE" 'del(.inbounds[] | select(.tag == $old))' --arg old "$old_tag" || return 1
        _manage_service restart || return 1
        "$SINGBOX_BIN" check -c "$CONFIG_FILE" -c "$RELAY_CONFIG_FILE" || return 1
        if [ "$INIT_SYSTEM" = systemd ]; then systemctl is-active --quiet sing-box || return 1; else rc-service sing-box status >/dev/null || return 1; fi
        _is_pid_file_running_cmd "$(_argo_pid_file "$PORT")" "$CLOUDFLARED_BIN" || return 1
        return 0
    }
    if migrate_port; then
        committed=1
        trap - EXIT HUP INT TERM
        _stop_argo_tunnel "$OLD_PORT" || echo '注意：旧隧道进程清理失败，请检查旧进程'
        echo '端口修改完成，请使用新节点链接'
        printf 'SB_WEB_RESULT='
        jq -c --arg t "$new_tag" '.[$t]' "$ARGO_METADATA_FILE"
    else
        rollback_port
        trap - EXIT HUP INT TERM
        return 1
    fi
}
