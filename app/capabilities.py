"""Read-only, bounded SSH preflight; no extra node dependencies or daemons."""
import json
import time
from fastapi import HTTPException
from . import db

SCRIPT=r'''set -eu
os=unknown;[ ! -r /etc/os-release ] || . /etc/os-release
os=$(printf '%s' "${ID:-unknown}" | tr -cd 'a-zA-Z0-9_-')
arch=$(uname -m | tr -cd 'a-zA-Z0-9_-')
init=unknown
if [ -d /run/systemd/system ];then init=systemd;elif command -v rc-service >/dev/null 2>&1;then init=openrc;fi
mem=$(awk '/MemTotal/{print $2}' /proc/meminfo)
for path in /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes;do
 if [ -r "$path" ];then limit=$(cat "$path");case "$limit" in ''|*[!0-9]*) ;; *) if [ "$limit" -gt 0 ] && [ "$limit" -lt $((mem*1024)) ];then mem=$((limit/1024));fi;;esac;fi
done
disk=$(df -Pk / | awk 'NR==2{print $4}')
case "$mem:$disk" in *[!0-9:]*) exit 1;;esac
container=unknown
[ ! -e /.dockerenv ] || container=docker
if [ -r /run/systemd/container ];then container=$(tr -cd 'a-zA-Z0-9_-' < /run/systemd/container);fi
if tr '\000' '\n' < /proc/1/environ 2>/dev/null | grep -q '^container=lxc';then container=lxc;fi
ipv6=false
if command -v ip >/dev/null 2>&1 && ip -6 addr show scope global 2>/dev/null | grep -qE 'inet6 [23]';then ipv6=true;fi
curl=false;command -v curl >/dev/null 2>&1 && curl=true
wget=false;command -v wget >/dev/null 2>&1 && wget=true
jq=false;command -v jq >/dev/null 2>&1 && jq=true
cap=$(awk '/CapEff/{print $2}' /proc/self/status | tr -cd 'a-fA-F0-9')
printf '{"os":"%s","arch":"%s","init":"%s","memory_kib":%s,"disk_free_kib":%s,"container":"%s","public_ipv6":%s,"curl":%s,"wget":%s,"jq":%s,"effective_capabilities":"%s"}\n' "$os" "$arch" "$init" "$mem" "$disk" "$container" "$ipv6" "$curl" "$wget" "$jq" "$cap"
'''


def inspect(t,server_id):
    from . import ssh_control
    code,out=ssh_control.command(t,SCRIPT,25)
    if code:raise HTTPException(409,'服务器只读能力检测未完成，请检查系统工具')
    try:
        data=json.loads(out)
        if not isinstance(data,dict) or len(out)>8000:raise ValueError()
        supported=data['os'] in ('alpine','debian','ubuntu') and data['arch'] in ('x86_64','amd64','aarch64','arm64') and data['init'] in ('systemd','openrc')
        data['protocols']=['vless-ws-argo','reality','hysteria2'] if supported else []
        data['warnings']=[]
        if data['memory_kib']<192*1024:data['warnings'].append('内存较小，安装与升级将按服务器串行执行')
        if data['disk_free_kib']<128*1024:data['warnings'].append('根分区可用空间不足 128 MiB')
        data['install_supported']=supported and data['disk_free_kib']>=64*1024
        data['checked_at']=db.utcnow()
    except (ValueError,KeyError,TypeError):raise HTTPException(409,'服务器能力检测返回格式无效') from None
    db.get_conn().execute('UPDATE ssh_servers SET capabilities=?,capabilities_at=? WHERE id=?',(json.dumps(data),time.time(),server_id));db.get_conn().commit()
    return data


def require_install(data):
    if not data.get('install_supported'):raise HTTPException(409,'当前系统、架构、初始化方式或可用磁盘不满足安装要求，请查看能力检测结果')


def cached(server):
    data=json.loads(server.get('capabilities','{}'))
    return {'data':data,'age_seconds':int(time.time()-server['capabilities_at']) if server.get('capabilities_at') else None,
            'stale':not server.get('capabilities_at') or time.time()-server['capabilities_at']>86400}
