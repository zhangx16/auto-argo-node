# Loaded by the panel installer. The original upstream installer remains available.
_panel_install_core() {
 local arch channel="${ARGO_CORE_CHANNEL:-minimal}" tmp expected name url version requested="${1:-latest}"
 case "$(uname -m)" in x86_64|amd64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo '精简/固定原版通道仅支持 amd64、arm64';return 1;; esac
 case "$channel" in minimal|original) ;; *) return 1;; esac
 [ -n "${ARGO_PANEL:-}" ] || { echo '缺少面板下载地址';return 1; }
 mkdir -p /var/tmp /usr/local/bin "$SINGBOX_DIR" || return 1
 tmp=$(mktemp -d /var/tmp/argo-core.XXXXXXXX) || return 1
 if ! curl -fsS --connect-timeout 10 --max-time 30 "$ARGO_PANEL/agent/releases/manifest.json" -o "$tmp/manifest";then rm -rf "$tmp";return 1;fi
 if [ -n "${ARGO_RELEASE_SHA256:-}" ];then
  printf '%s  %s\n' "$ARGO_RELEASE_SHA256" "$tmp/manifest" | sha256sum -c - || { rm -rf "$tmp";return 1; }
 fi
 name="sing-box-${channel}-${arch}"
 if [ "$requested" != latest ];then
  case "$requested" in 1.13.21) name="sing-box-${channel}-${requested}-${arch}";; *) rm -rf "$tmp";return 1;;esac
 fi
 expected=$(jq -er --arg n "$name" '.files[$n].sha256 | select(test("^[a-f0-9]{64}$"))' "$tmp/manifest") || { rm -rf "$tmp";return 1; }
 version=$(jq -er --arg name "$name" '.files[$name].version // .core_version' "$tmp/manifest") || { rm -rf "$tmp";return 1; }
 if [ -x "$SINGBOX_BIN" ] && printf '%s  %s\n' "$expected" "$SINGBOX_BIN" | sha256sum -c - >/dev/null 2>&1;then
  cp "$SINGBOX_BIN" "$tmp/core" || { rm -rf "$tmp";return 1; }
 elif ! curl -fsS --retry 2 --connect-timeout 10 --max-time 300 "$ARGO_PANEL/agent/releases/$name" -o "$tmp/core";then rm -rf "$tmp";return 1;fi
 if ! printf '%s  %s\n' "$expected" "$tmp/core" | sha256sum -c -;then rm -rf "$tmp";return 1;fi
 chmod 755 "$tmp/core"
 [ "$("$tmp/core" version | awk '/^sing-box version/{print $3}')" = "$version" ] || { rm -rf "$tmp";return 1; }
 if [ -s "$CONFIG_FILE" ];then
  local relay="$RELAY_CONFIG_FILE"
  if [ ! -s "$relay" ];then relay="$tmp/relay";printf '%s' '{"inbounds":[],"outbounds":[],"route":{"rules":[]}}' > "$relay";fi
  _check_combined_config_files "$tmp/core" "$CONFIG_FILE" "$relay" || { echo '配置不兼容，保留原核心';rm -rf "$tmp";return 1; }
 fi
 SINGBOX_UPDATE_HAD_OLD=false;SINGBOX_UPDATE_BACKUP=''
 if [ -f "$SINGBOX_BIN" ];then
  SINGBOX_UPDATE_HAD_OLD=true;SINGBOX_UPDATE_BACKUP="${SINGBOX_BIN}.rollback.$$"
  cp -p "$SINGBOX_BIN" "$SINGBOX_UPDATE_BACKUP" || { rm -rf "$tmp";return 1; }
 fi
 cp "$tmp/core" "${SINGBOX_BIN}.new" && chmod 755 "${SINGBOX_BIN}.new" && mv "${SINGBOX_BIN}.new" "$SINGBOX_BIN" || { rm -rf "$tmp";return 1; }
 jq -n --arg channel "$channel" --arg version "$version" --arg sha "$expected" '{channel:$channel,version:$version,sha256:$sha}' > "$SINGBOX_DIR/core-install.pending"
 SINGBOX_STAGED_VERSION="$version"
 rm -rf "$tmp"
}
_panel_commit_core() {
 local backup="$SINGBOX_DIR/core-rollback"
 if [ -n "${SINGBOX_UPDATE_BACKUP:-}" ] && [ -s "$SINGBOX_UPDATE_BACKUP" ];then
  mkdir -p "$backup";chmod 700 "$backup"
  mv "$SINGBOX_UPDATE_BACKUP" "$backup/sing-box"
  sha256sum "$backup/sing-box" > "$backup/sha256"
  [ ! -f "$SINGBOX_DIR/core-install.json" ] || cp "$SINGBOX_DIR/core-install.json" "$backup/installation.json"
  # Configurations are reference copies only; rollback validates the CURRENT config.
  for f in config.json relay.json;do [ ! -f "$SINGBOX_DIR/$f" ] || cp "$SINGBOX_DIR/$f" "$backup/$f";done
  chmod 600 "$backup"/*
 fi
 [ ! -s "$SINGBOX_DIR/core-install.pending" ] || mv "$SINGBOX_DIR/core-install.pending" "$SINGBOX_DIR/core-install.json"
 SINGBOX_UPDATE_BACKUP=''
}
_panel_rollback_core() {
 local backup="$SINGBOX_DIR/core-rollback" stage
 [ -s "$backup/sing-box" ] && sha256sum -c "$backup/sha256" || { echo '没有有效的核心回滚备份';return 1; }
 chmod 700 "$backup/sing-box"
 _check_combined_config_files "$backup/sing-box" "$CONFIG_FILE" "$RELAY_CONFIG_FILE" || { echo '旧核心不兼容当前配置，未覆盖配置';return 1; }
 stage="${SINGBOX_BIN}.before-rollback"
 cp -p "$SINGBOX_BIN" "$stage" || return 1
 cp "$backup/sing-box" "${SINGBOX_BIN}.new" && chmod 755 "${SINGBOX_BIN}.new" && mv "${SINGBOX_BIN}.new" "$SINGBOX_BIN" || return 1
 if ! _manage_service restart;then mv "$stage" "$SINGBOX_BIN";_manage_service restart || true;return 1;fi
 rm -f "$stage"
 if [ -s "$backup/installation.json" ];then cp "$backup/installation.json" "$SINGBOX_DIR/core-install.json";else printf '%s' '{"channel":"restored"}' > "$SINGBOX_DIR/core-install.json";fi
 echo '已恢复原核心，当前节点配置保持不变'
}
if [ "${ARGO_CORE_CHANNEL:-legacy}" != legacy ];then
 _install_sing_box() { _panel_install_core "$@"; }
 _commit_singbox_binary() { _panel_commit_core; }
fi
