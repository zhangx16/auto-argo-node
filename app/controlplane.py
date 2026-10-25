"""Explicit trusted callback origins shared by protocol/probe bootstraps."""
import shlex
import ipaddress
import re
from urllib.parse import urlsplit,urlencode
from fastapi import HTTPException
from . import db


def clean(value):
    value=value.strip().rstrip('/')
    if not value:return ''
    try:u=urlsplit(value);port=u.port
    except ValueError:raise HTTPException(422,'管理地址端口无效') from None
    if u.scheme not in ('https','http') or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
        raise HTTPException(422,'管理地址应为完整 HTTP(S) 来源地址，不含路径、账号或参数')
    if any(ord(c)<33 or ord(c)==127 for c in value) or len(value)>500:raise HTTPException(422,'管理地址无效')
    try:ipaddress.ip_address(u.hostname)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',u.hostname) or '..' in u.hostname:raise HTTPException(422,'管理地址主机名无效')
    return value


def for_node(node_id):
    from . import ssh_control
    return ssh_control.binding(node_id)


def for_host(host_id):
    r=db.get_conn().execute('SELECT * FROM ssh_servers WHERE monitor_host_id=?',(host_id,)).fetchone()
    return dict(r) if r else None


def origins(default,server=None):
    import json
    values=([server.get('control_url') or default]+json.loads(server.get('fallback_urls','[]'))) if server else []
    values=[clean(v) for v in values if v]
    if not values:values=[clean(default)]
    return list(dict.fromkeys(values))


def selected(default,server=None,requested=None):
    values=origins(default,server)
    if requested is None:return values[0]
    if requested not in values:raise HTTPException(400,'此地址未列入该服务器的管理通道')
    return requested


def command(path,default,server=None):
    values=origins(default,server)
    urls=[v+path+'?'+urlencode({'control':v}) for v in values]
    script='umask 077; f=$(mktemp); trap \'rm -f "$f"\' EXIT; ok=0; for u in '+ ' '.join(shlex.quote(u) for u in urls)+'''; do
 if command -v curl >/dev/null 2>&1; then curl --connect-timeout 8 --max-time 90 -fsSL "$u" -o "$f" && { ok=1; break; }; fi
 if command -v wget >/dev/null 2>&1; then wget -T 30 -q "$u" -O "$f" && { ok=1; break; }; fi
 done; [ "$ok" = 1 ] && sh "$f"'''
    return 'sh -c '+shlex.quote(script)


def runtime_selection(default,server):
    """Only configured origins are probed; no secrets are sent during selection."""
    values=origins(default,server)
    return '''CONTROL=""
for candidate in '''+' '.join(shlex.quote(v) for v in values)+'''; do
 if curl --connect-timeout 4 --max-time 6 -fsS "$candidate/health" >/dev/null 2>&1;then CONTROL="$candidate";break;fi
done
[ -n "$CONTROL" ] || { echo '管理主地址与备用通道均不可达';exit 1; }
'''
