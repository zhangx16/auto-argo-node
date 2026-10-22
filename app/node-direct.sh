BASE=/usr/local/lib/argo-deploy
if source "$BASE/library.sh" agent-library;then :;else exit 1;fi
SELF_SCRIPT_PATH=/usr/local/bin/sb
SCRIPT_DIR=/usr/local/bin
export GOMAXPROCS=1
_detect_init_system
TAG="panel-${KIND}-${NODE_ID}"
if [ "$OPERATION" = read ];then
 jq -e --arg t "$TAG" --arg u "$CREDENTIAL" '.inbounds[] | select(.tag==$t) | .users[] | select((.uuid // .password)==$u)' "$CONFIG_FILE" >/dev/null
 printf 'SB_WEB_RESULT=';jq -c . "$DIR/direct.json";exit 0
fi
case "$INIT_SYSTEM" in systemd|openrc) ;; *) echo '需要 systemd/OpenRC';exit 1;; esac
run_step() { if "$@";then return 0;else echo "步骤失败：$1";return 1;fi; }
run_step _install_dependencies
if [ ! -x "$SINGBOX_BIN" ];then run_step _do_update_singbox latest
else
 run_step _initialize_config_files
 run_step _init_relay_config
 run_step _ensure_relay_config
 [ -f "$SERVICE_FILE" ] || run_step _create_service_files
fi
{ cat "$BASE/library.sh";printf '\nmain\n'; } > /usr/local/bin/sb.new
chmod 700 /usr/local/bin/sb.new;mv /usr/local/bin/sb.new /usr/local/bin/sb
install_direct() {
 local net=tcp snapshot committed=0 oldport keypair private public sid cert key
 [ "$KIND" != hysteria2 ] || net=udp
 jq -e --arg t "$TAG" --arg u "$CREDENTIAL" '[.inbounds[]? | select(.tag==$t) | .users[]? | (.uuid // .password)] | all(.==$u)' "$CONFIG_FILE" >/dev/null || { echo '节点身份不一致';return 1; }
 oldport=$(jq -r --arg t "$TAG" '.inbounds[] | select(.tag==$t) | .listen_port' "$CONFIG_FILE") || return 1
 if [ "$oldport" != "$PORT" ] && _check_port_conflict "$PORT" "$net";then echo '监听端口冲突';return 1;fi
 snapshot=$(mktemp -d "$DIR/direct-backup.XXXXXXXX") || return 1
 cp "$CONFIG_FILE" "$snapshot/config" || return 1
 [ ! -f "$DIR/direct.json" ] || cp "$DIR/direct.json" "$snapshot/meta" || return 1
 rollback_direct() {
  local code=$?
  if [ "$committed" = 0 ];then
   cp "$snapshot/config" "$CONFIG_FILE"
   if [ -f "$snapshot/meta" ];then cp "$snapshot/meta" "$DIR/direct.json";else rm -f "$DIR/direct.json";fi
   _manage_service restart >/dev/null 2>&1 || true
  fi
  rm -rf "$snapshot";return "$code"
 }
 trap rollback_direct RETURN
 if [ "$KIND" = reality ];then
  private=$(jq -r --arg t "$TAG" '.inbounds[] | select(.tag==$t) | .tls.reality.private_key' "$CONFIG_FILE") || return 1
  if [ -s "$DIR/direct.json" ];then
   public=$(jq -r '.public_key' "$DIR/direct.json") || return 1
   sid=$(jq -r '.short_id' "$DIR/direct.json") || return 1
  else
   keypair=$("$SINGBOX_BIN" generate reality-keypair) || return 1
   private=$(awk '/PrivateKey/ {print $2}' <<< "$keypair")
   public=$(awk '/PublicKey/ {print $2}' <<< "$keypair")
   sid=$("$SINGBOX_BIN" generate rand --hex 8) || return 1
  fi
  [ -n "$private" ] && [ -n "$public" ] && [ -n "$sid" ] || return 1
  _atomic_modify_json "$CONFIG_FILE" '.inbounds = ([.inbounds[] | select(.tag!=$t)] + [{type:"vless",tag:$t,listen:"::",listen_port:$p,users:[{uuid:$u,flow:"xtls-rprx-vision"}],tls:{enabled:true,server_name:$s,reality:{enabled:true,handshake:{server:$s,server_port:443},private_key:$k,short_id:[$i]}}}])' --arg t "$TAG" --argjson p "$PORT" --arg u "$CREDENTIAL" --arg s "$SNI" --arg k "$private" --arg i "$sid" || return 1
 else
  cert="$SINGBOX_DIR/${TAG}.pem";key="$SINGBOX_DIR/${TAG}.key"
  [ -s "$cert" ] && [ -s "$key" ] || _generate_self_signed_cert "$SNI" "$cert" "$key" || return 1
  _atomic_modify_json "$CONFIG_FILE" '.inbounds = ([.inbounds[] | select(.tag!=$t)] + [{type:"hysteria2",tag:$t,listen:"::",listen_port:$p,users:[{password:$u}],tls:{enabled:true,server_name:$s,alpn:["h3"],certificate_path:$c,key_path:$k}}])' --arg t "$TAG" --argjson p "$PORT" --arg u "$CREDENTIAL" --arg s "$SNI" --arg c "$cert" --arg k "$key" || return 1
  public='';sid=''
 fi
 "$SINGBOX_BIN" check -c "$CONFIG_FILE" -c "$RELAY_CONFIG_FILE" || return 1
 _manage_service restart || return 1
 if [ "$INIT_SYSTEM" = systemd ];then systemctl is-active --quiet sing-box || return 1;else rc-service sing-box status >/dev/null || return 1;fi
 jq -cn --arg k "$KIND" --arg s "$SNI" --arg u "$CREDENTIAL" --argjson p "$PORT" --arg pb "$public" --arg si "$sid" '{protocol:$k,type:"direct",domain:$s,uuid:$u,local_port:$p,path:"/",public_key:$pb,short_id:$si}' > "$DIR/direct.json.new" || return 1
 chmod 600 "$DIR/direct.json.new";mv "$DIR/direct.json.new" "$DIR/direct.json" || return 1
 committed=1;trap - RETURN;rm -rf "$snapshot"
 printf 'SB_WEB_RESULT=';jq -c . "$DIR/direct.json"
}
if _with_state_lock install_direct;then exit 0;else exit 1;fi
