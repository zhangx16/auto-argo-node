#!/bin/sh
# Sourced by the minute worker. No daemon, package, network request or sleep.
resource_sample() {
 resource_now=$(date +%s)
 resource_cpu=$(awk '/^cpu / { total=0;for(i=2;i<=NF && i<=9;i++)total+=$i;print total, $5+$6;exit }' /proc/stat 2>/dev/null)
 resource_pct=null
 if [ -r "$DIR/resource-cpu" ];then
  resource_old=$(cat "$DIR/resource-cpu")
  resource_pct=$(printf '%s\n%s\n' "$resource_old" "$resource_cpu" | awk 'NR==1{t=$1;i=$2;next} $1>t{v=100*(1-($2-i)/($1-t));if(v>=0&&v<=100)printf "%.2f",v;else printf "null";ok=1} END{if(!ok)printf "null"}')
 fi
 printf '%s\n' "$resource_cpu" > "$DIR/resource-cpu.new" && mv "$DIR/resource-cpu.new" "$DIR/resource-cpu"
 awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2;has=1} /^MemFree:/{f=$2} /^Buffers:/{b=$2} /^Cached:/{c=$2} /^SwapTotal:/{st=$2} /^SwapFree:/{sf=$2} END{if(!has)a=f+b+c;printf "%.0f %.0f %.0f %.0f\n",t*1024,(t-a)*1024,st*1024,(st-sf)*1024}' /proc/meminfo > "$WORK/resource-mem"
 read -r resource_mt resource_mu resource_st resource_su < "$WORK/resource-mem"
 df -Pk / 2>/dev/null | awk 'NR>1{t=$2;u=$3} END{printf "%.0f %.0f\n",t*1024,u*1024}' > "$WORK/resource-disk"
 read -r resource_dt resource_du < "$WORK/resource-disk"
 # Read comm/status only: command lines and environment can contain credentials.
 resource_sb=0;resource_cf=0;resource_sbc=0;resource_cfc=0
 for resource_p in /proc/[0-9]*;do
  read -r resource_name < "$resource_p/comm" 2>/dev/null || continue
  case "$resource_name" in sing-box|cloudflared)
   resource_rss=$(awk '/^VmRSS:/{printf "%.0f",$2*1024}' "$resource_p/status" 2>/dev/null)
   case "$resource_rss" in ''|*[!0-9]*) continue;; esac
   if [ "$resource_name" = sing-box ];then resource_sb=$((resource_sb+resource_rss));resource_sbc=$((resource_sbc+1));else resource_cf=$((resource_cf+resource_rss));resource_cfc=$((resource_cfc+1));fi;;
  esac
 done
 resource_uptime=$(awk '{printf "%.0f",$1}' /proc/uptime)
 resource_load=$(awk '{print $1}' /proc/loadavg)
 resource_cores=$(awk '/^cpu[0-9]+ /{n++} END{print n+0}' /proc/stat)
 jq -n --argjson at "$resource_now" --argjson cpu "$resource_pct" --argjson cores "$resource_cores" --argjson load "$resource_load" \
 --argjson mt "$resource_mt" --argjson mu "$resource_mu" --argjson st "$resource_st" --argjson su "$resource_su" \
 --argjson dt "$resource_dt" --argjson du "$resource_du" --argjson uptime "$resource_uptime" \
 --argjson sb "$resource_sb" --argjson cf "$resource_cf" --argjson sbc "$resource_sbc" --argjson cfc "$resource_cfc" \
 '{sampled_at:$at,cpu_percent:$cpu,cpu_cores:$cores,load1:$load,memory_total:$mt,memory_used:$mu,swap_total:$st,swap_used:$su,disk_total:$dt,disk_used:$du,uptime_seconds:$uptime,singbox_rss:$sb,cloudflared_rss:$cf,singbox_processes:$sbc,cloudflared_processes:$cfc}' > "$WORK/resources.json"
}
