"""Authenticated, bounded SSH deployment jobs. SSH credentials stay in worker memory."""
import base64
import codecs
import hashlib
import io
import json
import re
import secrets
import shlex
import socket
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, quote, urlparse, parse_qs

import paramiko
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, field_validator
from . import db

REVISION = '5486a730555ee96730791062aefd440a7b892751'
SCRIPT = Path(__file__).parent / 'vendor' / 'singbox.sh'
SLOTS = threading.BoundedSemaphore(3)
START_LOCK = threading.Lock()

class Target(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    ssh_port: int = Field(default=22, ge=1, le=65535)

    @field_validator('host')
    @classmethod
    def host_valid(cls, value):
        if not re.fullmatch(r'[A-Za-z0-9:.\-]+', value) or value.startswith('-'):
            raise ValueError('请填写主机名或 IP，不含协议及路径')
        return value

class DeployBody(Target):
    operation: Literal['install', 'read', 'update'] = 'install'
    old_port: int | None = Field(default=None, ge=1024, le=65535, exclude=True)
    expected_uuid: str = Field(default='', exclude=True)
    username: str = Field(default='root', pattern=r'^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}$')
    password: SecretStr = Field(default=SecretStr(''), max_length=1024)
    private_key: SecretStr = Field(default=SecretStr(''), max_length=20000)
    key_passphrase: SecretStr = Field(default=SecretStr(''), max_length=1024)
    host_key: str = Field(min_length=20, max_length=4096)
    remark: str = Field(default='Argo VLESS', min_length=1, max_length=80)
    node_port: int = Field(default=18080, ge=1024, le=65535)
    client_address: str = Field(default='', max_length=253)
    client_port: int = Field(default=443)
    ws_path: str = Field(default='', max_length=128)
    uuid: str = Field(default='', max_length=36)

    @field_validator('client_address')
    @classmethod
    def address_valid(cls, value):
        if value and not re.fullmatch(r'[A-Za-z0-9:.\-]+', value):
            raise ValueError('优选地址应为域名或 IP')
        return value

    @field_validator('client_port')
    @classmethod
    def port_valid(cls, value):
        if value not in (443, 2053, 2083, 2087, 2096, 8443):
            raise ValueError('请选择 Cloudflare 支持的 HTTPS 端口')
        return value

    @field_validator('ws_path')
    @classmethod
    def path_valid(cls, value):
        if value and not re.fullmatch(r'/[A-Za-z0-9/_\-]{1,127}', value):
            raise ValueError('WS 路径须以 / 开头，仅支持字母、数字、/、_、-')
        return value

    @field_validator('uuid')
    @classmethod
    def uuid_valid(cls, value):
        if value:
            from uuid import UUID
            value = str(UUID(value))
        return value


class NodeEditBody(BaseModel):
    remark: str = Field(min_length=1, max_length=80)
    node_port: int = Field(ge=1024, le=65535)
    client_address: str = Field(default='', max_length=253)
    client_port: int = Field(default=443)

    @field_validator('remark')
    @classmethod
    def name_valid(cls, value):
        if not value.strip():
            raise ValueError('节点名称不能为空')
        return value.strip()

    @field_validator('client_address')
    @classmethod
    def address_valid(cls, value):
        return DeployBody.address_valid(value.strip())

    @field_validator('client_port')
    @classmethod
    def port_valid(cls, value):
        return DeployBody.port_valid(value)


def save_settings(node_id, body):
    node = db.get_node(node_id)
    if not node:
        raise HTTPException(404, '节点不存在')
    node = enrich(node)
    if node.get('management') == 'agent':
        from . import agent
        return agent.edit(node_id, body)
    if node.get('protocol') != 'vless-ws-argo':
        raise HTTPException(400, '此编辑功能适用于 VLESS-WS + Argo 节点')
    if node['status'] == 'installing':
        raise HTTPException(409, '任务运行中，请等待完成后编辑')
    if node['share_link'] and body.node_port != node['node_port']:
        raise HTTPException(409, '修改已部署节点的监听端口需要 SSH 认证')
    link = node['share_link']
    if link:
        path = node['ws_path'] or parse_qs(urlparse(link).query).get('path', [''])[0]
        link = make_link({'domain':node['sni'], 'uuid':node['uuid'], 'path':path}, body)
    c = db.get_conn()
    try:
        c.execute('UPDATE nodes SET remark=?, node_port=?, client_address=?, share_link=?, updated_at=? WHERE id=?',
                  (body.remark, body.node_port, body.client_address, link, db.utcnow(), node_id))
        c.execute('UPDATE deployments SET client_port=? WHERE node_id=?', (body.client_port,node_id))
        c.commit()
    except Exception:
        c.rollback()
        raise
    db.add_log(node_id, '已更新名称、优选地址或客户端端口；节点链接已同步')
    return enrich(db.get_node(node_id))


def transport(target):
    sock = socket.create_connection((target.host, target.ssh_port), timeout=15)
    t = paramiko.Transport(sock)
    t.auth_timeout = 20
    t.banner_timeout = 15
    try:
        t.start_client(timeout=15)
        return t
    except Exception:
        t.close()
        raise


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


def probe(target):
    t = transport(target)
    try:
        key = t.get_remote_server_key()
        return {'host_key': key.get_base64(), 'fingerprint': fingerprint(key), 'algorithm': key.get_name()}
    finally:
        t.close()


def init():
    c = db.get_conn()
    c.execute('''CREATE TABLE IF NOT EXISTS deployments (
        node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
        host TEXT NOT NULL, ssh_port INTEGER NOT NULL, client_port INTEGER NOT NULL,
        ws_path TEXT NOT NULL, fingerprint TEXT NOT NULL)''')
    agent_filter = " AND id NOT IN (SELECT node_id FROM node_agents)" if c.execute("SELECT 1 FROM sqlite_master WHERE name='node_agents'").fetchone() else ""
    c.execute("UPDATE nodes SET status='error', last_error='面板重启中断了部署任务；请检查远端后重新部署' WHERE status='installing' AND id IN (SELECT node_id FROM deployments)" + agent_filter)
    c.commit()


def enrich(node):
    row = db.get_conn().execute('SELECT * FROM deployments WHERE node_id=?', (node['id'],)).fetchone()
    if row:
        node.update(dict(row))
        node['protocol'] = 'vless-ws-argo'
    from . import agent
    return agent.enrich(node)


def installer_text():
    text = (Path(__file__).parent / "node-library.sh").read_text()
    marker = "jq -c 'to_entries[]' \"$ARGO_METADATA_FILE\""
    if text.count(marker) != 1:
        raise RuntimeError('上游守护脚本结构已变化，拒绝部署')
    return text.replace(marker, "jq -c 'to_entries[] | select(.value.manager != \"argo-deploy-fixed\")' \"$ARGO_METADATA_FILE\"")


def driver(body):
    """Pass only validated configuration and a vendored installer, never SSH secrets."""
    if body.operation == 'read':
        return "#!/bin/sh\nset -eu\n[ \"$(id -u)\" = 0 ] || exit 1\nprintf 'SB_WEB_RESULT='\njq -ce --arg t " + shlex.quote('argo-vless-ws-' + str(body.node_port)) + " '.[$t] | select(.domain and .uuid and .path)' /usr/local/etc/sing-box/argo_metadata.json\n"
    assignments = '\n'.join(f'{k}={shlex.quote(str(v))}' for k,v in {
        'PORT': body.node_port, 'WS': body.ws_path or '/' + secrets.token_hex(12),
        'NAME': body.remark, 'CUSTOM_UUID': body.uuid, 'OPERATION':body.operation,
        'OLD_PORT':body.old_port or body.node_port, 'EXPECTED_UUID':body.expected_uuid,
    }.items())
    encoded = base64.b64encode(installer_text().encode()).decode()
    port_helper = (Path(__file__).parent / 'port-update.sh').read_text()
    return '''#!/bin/sh
set -eu
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ "$(id -u)" = 0 ] || { echo 'ERROR: 当前版本要求 root SSH 用户'; exit 1; }
. /etc/os-release
case "$ID" in
 alpine) command -v bash >/dev/null && command -v curl >/dev/null || apk add --no-cache bash curl ca-certificates ;;
 debian|ubuntu) command -v bash >/dev/null && command -v curl >/dev/null || { apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y bash curl ca-certificates; } ;;
 *) echo 'ERROR: 当前仅支持 Alpine / Debian / Ubuntu'; exit 1 ;;
esac
case "$(uname -m)" in x86_64|amd64|aarch64|arm64|armv7l) ;; *) echo 'ERROR: 不支持的 CPU 架构'; exit 1 ;; esac
echo "系统：$ID $(uname -m)"
WORK=$(mktemp -d /tmp/sb-web.XXXXXXXX)
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
base64 -d > "$WORK/singbox.sh" <<'SB_PAYLOAD'
''' + encoded + '''
SB_PAYLOAD
# Remove only the upstream interactive entry point; retain all functions.
test "$(tail -n 1 "$WORK/singbox.sh")" = main
sed '$d' "$WORK/singbox.sh" > "$WORK/library.sh"
cat > "$WORK/port-update.sh" <<'SB_PORT_HELPER'
''' + port_helper + '''
SB_PORT_HELPER
cat > "$WORK/run.sh" <<'SB_DRIVER'
#!/bin/bash
set -e
''' + assignments + '''
WORK="$1"
source "$WORK/library.sh" web-library
SCRIPT_DIR=/usr/local/bin
SELF_SCRIPT_PATH=/usr/local/bin/sb
# Upstream functions assume errexit is disabled internally. Preserve their explicit
# error handling, while failing the outer driver if a complete step fails.
run_step() { if "$@"; then return 0; else echo "ERROR: 步骤失败: $1"; return 1; fi; }
GITHUB_RAW_BASE=https://raw.githubusercontent.com/0xdabiaoge/singbox-lite/''' + REVISION + '''
TAG="argo-vless-ws-${PORT}"
_check_root
_detect_init_system
case "$INIT_SYSTEM" in systemd|openrc) ;; *) echo 'ERROR: 缺少 systemd/OpenRC，无法保证开机启动'; exit 1 ;; esac
mkdir -p "$SINGBOX_DIR"
if [ "$OPERATION" = update ]; then
 source "$WORK/port-update.sh"
 run_step update_port
 exit $?
fi
if [ -s "$ARGO_METADATA_FILE" ] && command -v jq >/dev/null && jq -e --arg t "$TAG" 'has($t)' "$ARGO_METADATA_FILE" >/dev/null; then
 echo 'ERROR: 该端口已有 Argo 节点，请使用新端口'; exit 1
fi
# Reject occupied ports instead of allowing the interactive installer to pick a random one.
if { command -v ss >/dev/null && ss -lnt; } 2>/dev/null | grep -qE ":${PORT}[[:space:]]"; then
 echo 'ERROR: 监听端口已占用'; exit 1
fi
if { command -v netstat >/dev/null && netstat -lnt; } 2>/dev/null | grep -qE ":${PORT}[[:space:]]"; then
 echo 'ERROR: 监听端口已占用'; exit 1
fi
run_step _install_dependencies
if _check_port_conflict "$PORT" tcp; then echo 'ERROR: 端口与现有配置冲突'; exit 1; fi
cp "$WORK/singbox.sh" /usr/local/bin/sb
chmod 700 /usr/local/bin/sb
if [ ! -x "$SINGBOX_BIN" ]; then
 run_step _do_update_singbox latest
else
 run_step _initialize_config_files
 run_step _init_relay_config
 run_step _ensure_relay_config
 [ -f "$SERVICE_FILE" ] || run_step _create_service_files
fi
# The web application returns the link itself; do not print QR codes or generate subscriptions.
_show_node_link() { :; }
printf '%s\\n' "$PORT" "$WS" 1 "$NAME" > "$WORK/answers"
run_step _add_argo_vless_ws < "$WORK/answers"
jq -e --arg t "$TAG" '.[$t].domain | strings | select(length>0)' "$ARGO_METADATA_FILE" >/dev/null
if [ -n "$CUSTOM_UUID" ]; then
 _atomic_modify_json "$CONFIG_FILE" '.inbounds |= map(if .tag == $t then .users[0].uuid = $u else . end)' --arg t "$TAG" --arg u "$CUSTOM_UUID"
 _atomic_modify_json "$ARGO_METADATA_FILE" '.[$t].uuid = $u' --arg t "$TAG" --arg u "$CUSTOM_UUID"
 export NODE_UUID="$CUSTOM_UUID" NEW_NAME="$NAME"
 run_step _atomic_modify_yaml "$CLASH_YAML_FILE" '(.proxies[] | select(.name == env(NEW_NAME))).uuid = env(NODE_UUID)'
 run_step _manage_service restart
fi
"$SINGBOX_BIN" check -c "$CONFIG_FILE" -c "$RELAY_CONFIG_FILE"
if [ "$INIT_SYSTEM" = systemd ]; then systemctl is-active --quiet sing-box; else rc-service sing-box status >/dev/null; fi
PFILE=$(_argo_pid_file "$PORT")
_is_pid_file_running_cmd "$PFILE" "$CLOUDFLARED_BIN"
printf 'SB_WEB_RESULT='
jq -c --arg t "$TAG" '.[$t]' "$ARGO_METADATA_FILE"
SB_DRIVER
bash "$WORK/run.sh" "$WORK"
'''


def make_link(meta, body):
    domain = meta['domain']
    if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
        raise ValueError('返回的隧道域名无效')
    from uuid import UUID
    uuid = str(UUID(meta['uuid']))
    host = body.client_address or domain
    if ':' in host:
        host = '[' + host + ']'
    # Match upstream sing-box WebSocket early-data settings.
    path = meta['path']
    if 'ed=' not in path:
        path += ('&' if '?' in path else '?') + 'ed=2560'
    params = urlencode({'encryption':'none', 'security':'tls', 'sni':domain,
                        'type':'ws', 'host':domain, 'path':path})
    return f'vless://{uuid}@{host}:{body.client_port}?{params}#{quote(body.remark, safe="")}'


def authenticate_password(transport, username, password):
    try:
        return transport.auth_password(username, password, fallback=False)
    except paramiko.BadAuthenticationType as exc:
        if 'keyboard-interactive' not in exc.allowed_types:
            raise
    challenge_error = None
    def respond(title, instructions, prompts):
        nonlocal challenge_error
        text = (title + ' ' + instructions + ' ' + ' '.join(p[0] for p in prompts)).lower()
        if len(prompts) > 1 or any(word in text for word in ('verification code', 'one-time', 'otp', 'authenticator', '验证码')):
            challenge_error = '服务器要求验证码或多项交互认证，当前不能仅使用 root 密码自动登录'
            return []
        if any(word in text for word in ('new password', 'password expired', '新密码', '密码过期')):
            challenge_error = '服务器要求更新已过期密码，请先通过 SSH 终端完成改密后重试'
            return []
        return [password] if prompts else []
    try:
        return transport.auth_interactive(username, respond)
    except (paramiko.SSHException, EOFError):
        if challenge_error:
            raise ValueError(challenge_error) from None
        raise ValueError('SSH 交互认证未通过：请核对 root 密码，并确认该服务器允许 root 通过 SSH 登录；请使用同一地址和 SSH 端口在终端验证') from None


def worker(node_id, body):
    t = None
    try:
        db.add_log(node_id, '正在验证 SSH 主机密钥并连接服务器')
        t = transport(body)
        if not secrets.compare_digest(t.get_remote_server_key().get_base64(), body.host_key):
            raise ValueError('SSH 主机密钥已变化，请重新检查服务器指纹')
        key_data = body.private_key.get_secret_value()
        db.add_log(node_id, f'SSH 用户名：{body.username}；认证方式：' + ('私钥' if key_data else '密码（必要时尝试交互认证）'))
        if key_data:
            key = None
            for kind in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    key = kind.from_private_key(io.StringIO(key_data), password=body.key_passphrase.get_secret_value() or None)
                    break
                except (paramiko.SSHException, ValueError):
                    continue
            if key is None:
                raise ValueError('私钥格式或私钥口令不正确')
            t.auth_publickey(body.username, key)
        else:
            authenticate_password(t, body.username, body.password.get_secret_value())
        if not t.is_authenticated():
            raise ValueError('SSH 认证尚未完成：服务器可能要求额外的私钥、密码或验证码；当前不支持多步骤认证')
        # Discard credential fields immediately after authentication.
        body.password = SecretStr(''); body.private_key = SecretStr(''); body.key_passphrase = SecretStr('')
        db.add_log(node_id, 'SSH 已连接，正在读取现有节点元数据' if body.operation == 'read' else ('SSH 已连接，正在修改远端监听端口并验证新隧道' if body.operation == 'update' else 'SSH 已连接，正在安装依赖、sing-box 和临时隧道（最长 25 分钟）'))
        channel = t.open_session(timeout=20)
        channel.settimeout(30)
        channel.exec_command('umask 077; f=$(mktemp /tmp/sb-web-entry.XXXXXXXX); cat > "$f"; trap \'rm -f "$f"\' EXIT; timeout 1500 sh "$f"')
        channel.sendall(driver(body).encode())
        channel.shutdown_write()
        deadline = time.monotonic() + 1540
        buffers = {'stdout': '', 'stderr': ''}
        decoders = {name: codecs.getincrementaldecoder('utf-8')(errors='replace') for name in buffers}
        result = None
        def process(line):
            nonlocal result
            line = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line).strip()
            if line.startswith('SB_WEB_RESULT='):
                result = json.loads(line.split('=', 1)[1])
            elif line:
                db.add_log(node_id, line[:2000])
        while True:
            if time.monotonic() > deadline:
                channel.close()
                raise TimeoutError('部署超时，请查看日志和远端服务状态')
            read = False
            for stream, ready, recv in (('stdout', channel.recv_ready, channel.recv), ('stderr', channel.recv_stderr_ready, channel.recv_stderr)):
                if ready():
                    buffers[stream] += decoders[stream].decode(recv(16384))
                    read = True
                    while '\n' in buffers[stream]:
                        line, buffers[stream] = buffers[stream].split('\n', 1)
                        process(line)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if not read:
                time.sleep(.2)
        for stream, buffer in buffers.items():
            buffer += decoders[stream].decode(b'', final=True)
            if buffer:
                process(buffer)
        code = channel.recv_exit_status()
        if code or result is None:
            raise RuntimeError(f'远端安装失败（退出码 {code}），请查看日志；远端可能已完成部分步骤')
        if body.expected_uuid and result['uuid'] != body.expected_uuid:
            raise ValueError('远端节点 UUID 与原记录不一致，请检查目标服务器')
        link = make_link(result, body)
        db.update_node(node_id, status='online', remark=body.remark, node_port=body.node_port, client_address=body.client_address, share_link=link, uuid=result['uuid'], sni=result['domain'], last_seen=db.utcnow(), last_error='')
        c = db.get_conn()
        c.execute('UPDATE deployments SET client_port=?, ws_path=? WHERE node_id=?', (body.client_port,result['path'],node_id))
        c.commit()
        db.add_log(node_id, '已读取当前链接；本次未重新安装或验证服务进程。' if body.operation == 'read' else '部署完成：服务及隧道进程已验证。请从客户端验证实际网络连接；临时隧道重建后需重新读取链接。')
    except paramiko.BadAuthenticationType as exc:
        methods = '、'.join(exc.allowed_types)
        message = f'SSH 认证失败：服务器允许 {methods}。'
        if not key_data and 'keyboard-interactive' in exc.allowed_types:
            message += '密码交互认证未通过；请核对用户名及密码，或使用私钥。需要额外验证码的登录暂不支持。'
        else:
            message += '服务器不接受当前认证方式，请使用支持的方式登录。'
        db.update_node(node_id, status='error', last_error=message)
        db.add_log(node_id, message)
    except paramiko.AuthenticationException:
        db.update_node(node_id, status='error', last_error='SSH 认证失败，请检查用户名、密码或私钥')
        db.add_log(node_id, 'SSH 认证失败')
    except Exception as exc:
        # Do not include arbitrary exception strings which could contain credential material.
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError, TimeoutError)) else 'SSH 连接或远端执行失败：' + type(exc).__name__
        db.update_node(node_id, status='error', last_error=message)
        db.add_log(node_id, message)
    finally:
        body.password = SecretStr(''); body.private_key = SecretStr(''); body.key_passphrase = SecretStr('')
        if t:
            t.close()
        SLOTS.release()


def register(app, require_login):
    def check(request):
        require_login(request)
        origin = request.headers.get('origin')
        from urllib.parse import urlparse
        if origin and urlparse(origin).netloc != request.headers.get('host'):
            raise HTTPException(403, '请求来源不匹配')

    @app.post('/api/ssh/probe')
    def ssh_probe(body: Target, request: Request):
        check(request)
        try:
            return probe(body)
        except Exception:
            raise HTTPException(400, '无法连接 SSH，请检查地址、端口及防火墙')

    def start_locked(body: DeployBody, request: Request, node_id=None):
        check(request)
        if body.username != 'root':
            raise HTTPException(400, '当前版本需要 root SSH 用户，暂不支持交互式 sudo')
        if not body.password.get_secret_value() and not body.private_key.get_secret_value():
            raise HTTPException(400, '请填写 SSH 密码或私钥')
        node = None
        body.old_port = None
        body.expected_uuid = ''
        if node_id is not None:
            node = db.get_node(node_id)
            if not node:
                raise HTTPException(404, '节点不存在')
            node = enrich(node)
            if node.get('management') == 'agent':
                raise HTTPException(409, '此节点已接入主动上报，请使用节点配置任务')
            if node.get('protocol') != 'vless-ws-argo':
                raise HTTPException(400, '仅支持管理 Argo 节点')
            if node['status'] == 'installing':
                raise HTTPException(409, '任务正在运行，请稍后操作')
            if body.host != node['host'] or body.ssh_port != node['ssh_port']:
                raise HTTPException(400, '服务器地址与原记录不一致')
            body.old_port = node['node_port']
            body.expected_uuid = node['uuid']
            if body.operation == 'read':
                body.node_port = node['node_port']
            if body.operation == 'update' and not node['share_link']:
                raise HTTPException(400, '节点尚未部署成功，请选择重新部署或读取链接')
            if body.operation == 'install' and node['share_link']:
                raise HTTPException(409, '节点已经部署，请使用编辑功能')
        elif body.operation == 'update':
            raise HTTPException(400, '请指定要修改的节点')
        existing = db.get_conn().execute("SELECT 1 FROM deployments d JOIN nodes n ON n.id=d.node_id WHERE d.host=? AND d.ssh_port=? AND n.status='installing'", (body.host, body.ssh_port)).fetchone()
        if existing:
            raise HTTPException(409, '这台服务器已有任务运行，请等待完成')
        try:
            base64.b64decode(body.host_key, validate=True)
        except Exception:
            raise HTTPException(400, '无效的 SSH 主机密钥，请重新检查指纹')
        if not SLOTS.acquire(blocking=False):
            raise HTTPException(429, '已有 3 个部署任务运行，请稍后重试')
        try:
            if node is None:
                node = db.create_node(body.remark, body.node_port, '', body.client_address, secrets.token_urlsafe(24))
            key = base64.b64decode(body.host_key, validate=True)
            fp = 'SHA256:' + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip('=')
            c = db.get_conn()
            c.execute('INSERT INTO deployments VALUES (?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET fingerprint=excluded.fingerprint', (node['id'], body.host, body.ssh_port, body.client_port, body.ws_path, fp))
            c.commit()
            db.update_node(node['id'], status='installing', host_ip=body.host)
            threading.Thread(target=worker, args=(node['id'],body), daemon=True).start()
            return {'id':node['id'], 'status':'installing'}
        except Exception:
            SLOTS.release()
            raise

    @app.post('/api/deploy')
    def start(body: DeployBody, request: Request):
        with START_LOCK:
            return start_locked(body, request)

    @app.post('/api/deploy/{node_id}')
    def restart_or_update(node_id: int, body: DeployBody, request: Request):
        with START_LOCK:
            return start_locked(body, request, node_id)

    @app.patch('/api/deployments/{node_id}')
    def edit_settings(node_id: int, body: NodeEditBody, request: Request):
        check(request)
        with START_LOCK:
            return save_settings(node_id, body)
