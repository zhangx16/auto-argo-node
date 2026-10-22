#!/bin/sh
# Sourced by the existing one-shot agent. Kernel counters; no daemon, packet capture,
# per-packet userspace work, quota/drop/accept rules, or system firewall changes.
traffic_sample() {
 printf null > "$WORK/traffic.json"
 command -v nft >/dev/null 2>&1 || return 0
 case "$NODE_ID:$PORT" in *[!0-9:]*|'') return 0;; esac
 table="argo_traffic_${NODE_ID}"
 generation="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null):${PORT}"
 [ -n "$generation" ] || return 0
 nft -j list table inet "$table" > "$WORK/counters.json" 2>/dev/null || :
 saved=$(cat "$DIR/traffic-generation" 2>/dev/null || true)
 if [ "$saved" != "$generation" ] || ! jq -e '[.nftables[]?.counter? | select(.name=="rx" or .name=="tx")] | length==2' "$WORK/counters.json" >/dev/null 2>&1;then
  if [ "$PROTOCOL" = hysteria2 ];then transport=udp;else transport=tcp;fi
  # Only the dedicated table owned by this exact node is ever replaced.
  if nft list table inet "$table" >/dev/null 2>&1;then printf 'delete table inet %s\n' "$table" > "$WORK/traffic.nft";else : > "$WORK/traffic.nft";fi
  cat >> "$WORK/traffic.nft" <<EOF
add table inet $table
add counter inet $table rx
add counter inet $table tx
add chain inet $table incoming { type filter hook input priority -10; policy accept; }
add chain inet $table outgoing { type filter hook output priority -10; policy accept; }
add rule inet $table incoming $transport dport $PORT counter name rx
add rule inet $table outgoing $transport sport $PORT counter name tx
EOF
  nft -f "$WORK/traffic.nft" >/dev/null 2>&1 || return 0
  printf '%s\n' "$generation" > "$DIR/traffic-generation"
  # Counter recreation, including firewall flush, must create a fresh generation.
  cat /proc/sys/kernel/random/uuid > "$DIR/traffic-epoch"
  nft -j list table inet "$table" > "$WORK/counters.json" 2>/dev/null || return 0
 fi
 epoch=$(cat "$DIR/traffic-epoch" 2>/dev/null || true)
 [ -n "$epoch" ] || return 0
 jq --arg epoch "$epoch" '[.nftables[]?.counter? | select(.name=="rx" or .name=="tx")] | {epoch:$epoch,scope:"port",rx:(map(select(.name=="rx"))[0].bytes),tx:(map(select(.name=="tx"))[0].bytes)} | select(.rx!=null and .tx!=null)' "$WORK/counters.json" > "$WORK/traffic.json" 2>/dev/null || printf null > "$WORK/traffic.json"
 [ -s "$WORK/traffic.json" ] || printf null > "$WORK/traffic.json"
}
