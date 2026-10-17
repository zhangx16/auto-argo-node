import os
import secrets
import shlex
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import auth, db, deploy, agent
from .scripts import INSTALL_SH, UNINSTALL_SH, render_script

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SINGBOX_MGMT_DATA", "/app/data"))
STATIC_DIR = APP_DIR / "static"
CREDENTIALS_FILE = DATA_DIR / "credentials.txt"
SECRET_FILE = DATA_DIR / "secret.key"
DEFAULT_PANEL_URL = os.environ.get("PANEL_URL", "https://panel.example.com").rstrip("/")

app = FastAPI(title="singbox-mgmt", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def public_url() -> str:
    saved = db.get_setting("panel_url")
    if saved:
        return saved.rstrip("/")
    return DEFAULT_PANEL_URL


def origin_ip() -> str:
    return db.get_setting("origin_ip") or os.environ.get("ORIGIN_IP", "")


def panel_host() -> str:
    return urlparse(public_url()).hostname or "panel.example.com"


def curl_fetch(url: str) -> str:
    host = panel_host()
    ip = origin_ip()
    resolve = f" --resolve {shlex.quote(f'{host}:443:{ip}')}" if ip else ""
    return f"curl -4 --http1.1 --tlsv1.2 -fsSL{resolve} {shlex.quote(url)}"


def install_command(token: str) -> str:
    return f"{curl_fetch(f'{public_url()}/b/{token}.sh')} | bash"


def uninstall_command(token: str) -> str:
    return f"{curl_fetch(f'{public_url()}/u/{token}.sh')} | bash"


def ensure_secret() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    SECRET_FILE.write_text(secret + "\n", encoding="utf-8")
    SECRET_FILE.chmod(0o600)
    return secret


def ensure_admin() -> None:
    if db.get_setting("admin_user"):
        return
    user = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)
    salt = secrets.token_hex(16)
    db.set_setting("admin_user", user)
    db.set_setting("admin_salt", salt)
    db.set_setting("admin_hash", auth.hash_password(password, salt))
    CREDENTIALS_FILE.write_text(
        f"username={user}\npassword={password}\npanel={public_url()}\n"
        "note=登录后必须绑定 2FA；此明文密码仅首次生成。\n",
        encoding="utf-8",
    )
    CREDENTIALS_FILE.chmod(0o600)
    print("=" * 48, flush=True)
    print("  singbox-mgmt 已启动", flush=True)
    print(f"  面板: {public_url()}", flush=True)
    print(f"  用户: {user}", flush=True)
    print(f"  密码: {password}", flush=True)
    print("  首次登录后必须绑定 TOTP", flush=True)
    print("=" * 48, flush=True)


@app.on_event("startup")
def on_startup() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    agent.init()
    deploy.init()
    agent.start_loop()
    ensure_admin()
    db.set_setting("panel_url", DEFAULT_PANEL_URL)
    if not db.get_setting("origin_ip"):
        db.set_setting("origin_ip", os.environ.get("ORIGIN_IP", ""))
    if not db.get_setting("sub_token"):
        db.set_setting("sub_token", secrets.token_urlsafe(18))


app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["127.0.0.1", "::1"])
app.add_middleware(
    SessionMiddleware,
    secret_key=ensure_secret(),
    session_cookie="sbmgmt",
    same_site="strict",
    https_only=True,
    max_age=60 * 60 * 12,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def require_login(request: Request) -> str:
    if not request.session.get("authed") or not request.session.get("user"):
        raise HTTPException(status_code=401, detail="未登录")
    return request.session["user"]


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TotpBody(BaseModel):
    challenge: str = Field(min_length=8, max_length=128)
    code: str = Field(min_length=6, max_length=19)


class NodeCreate(BaseModel):
    remark: str = Field(min_length=1, max_length=80)
    node_port: int = Field(ge=1, le=65535)
    sni: str = Field(default="www.amd.com", max_length=253)
    client_address: str = Field(default="", max_length=253)


class NodePatch(BaseModel):
    remark: str | None = Field(default=None, min_length=1, max_length=80)
    share_link: str | None = None
    sni: str | None = None
    client_address: str | None = None


class SettingsBody(BaseModel):
    panel_url: str | None = None
    origin_ip: str | None = None
    admin_password: str | None = None
    current_password: str | None = None
    totp_code: str | None = None


class TotpCodeBody(BaseModel):
    code: str = Field(min_length=6, max_length=19)


def enrich(node: dict) -> dict:
    item = dict(node)
    item["install_cmd"] = install_command(node["token"])
    item["uninstall_cmd"] = uninstall_command(node["token"])
    return deploy.enrich(item)


def generic_login_error() -> HTTPException:
    return HTTPException(status_code=400, detail="用户名、密码或验证码错误")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index(request: Request):
    if request.session.get("authed"):
        return RedirectResponse("/static/index.html")
    return RedirectResponse("/static/login.html")


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = auth.client_ip(request)
    username = body.username.strip()
    if auth.is_locked(ip, username):
        raise HTTPException(status_code=429, detail="尝试过多，请 15 分钟后再试")
    user = db.get_setting("admin_user")
    salt = db.get_setting("admin_salt")
    stored = db.get_setting("admin_hash")
    digest = auth.hash_password(body.password, salt)
    if username != user or not hmac_ok(digest, stored):
        auth.record_attempt(ip, username, False)
        raise generic_login_error()
    auth.record_attempt(ip, username, True)
    if not auth.totp_enabled():
        secret = auth.new_totp_secret()
        challenge = auth.create_challenge("setup", ip, extra=secret)
        otpauth = auth.otpauth_url(user, secret)
        return {
            "step": "setup_2fa",
            "challenge": challenge,
            "qr": auth.qr_data_url(otpauth),
            "secret": secret,
        }
    challenge = auth.create_challenge("login", ip)
    return {"step": "totp", "challenge": challenge}


def hmac_ok(left: str, right: str) -> bool:
    import hmac as hmac_mod

    if not left or not right:
        return False
    return hmac_mod.compare_digest(left, right)


@app.post("/api/login/totp")
def login_totp(body: TotpBody, request: Request):
    ip = auth.client_ip(request)
    if auth.is_locked(ip, db.get_setting("admin_user")):
        raise HTTPException(status_code=429, detail="尝试过多，请 15 分钟后再试")
    row = auth.take_challenge(body.challenge, "login", ip)
    if not row:
        auth.record_attempt(ip, db.get_setting("admin_user"), False)
        raise generic_login_error()
    secret = db.get_setting("totp_secret")
    ok = auth.verify_totp(secret, body.code) or auth.verify_backup_code(body.code)
    if not ok:
        auth.record_attempt(ip, db.get_setting("admin_user"), False)
        raise generic_login_error()
    auth.establish_session(request, db.get_setting("admin_user"))
    return {"ok": True, "user": db.get_setting("admin_user")}


@app.post("/api/login/setup-2fa")
def setup_2fa(body: TotpBody, request: Request):
    ip = auth.client_ip(request)
    row = auth.get_challenge(body.challenge, "setup")
    if not row:
        raise HTTPException(status_code=400, detail="绑定会话已过期，请重新登录")
    secret = row.get("extra") or ""
    if not auth.verify_totp(secret, body.code):
        raise HTTPException(status_code=400, detail="验证码不正确")
    auth.consume_challenge(body.challenge)
    db.set_setting("totp_secret", secret)
    codes = auth.generate_backup_codes()
    auth.establish_session(request, db.get_setting("admin_user"))
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text(
            f"username={db.get_setting('admin_user')}\n"
            f"panel={public_url()}\n"
            "note=明文密码已不再保存；两步验证已启用。\n",
            encoding="utf-8",
        )
        CREDENTIALS_FILE.chmod(0o600)
    return {"ok": True, "backup_codes": codes}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = require_login(request)
    return {
        "user": user,
        "panel_url": public_url(),
        "origin_ip": origin_ip(),
        "sub_url": f"{public_url()}/sub/{db.get_setting('sub_token')}",
        "totp_enabled": auth.totp_enabled(),
        "backup_remaining": auth.unused_backup_count(),
    }


@app.get("/api/settings")
def get_settings(request: Request):
    require_login(request)
    return {
        "panel_url": public_url(),
        "origin_ip": origin_ip(),
        "sub_url": f"{public_url()}/sub/{db.get_setting('sub_token')}",
        "admin_user": db.get_setting("admin_user"),
        "totp_enabled": auth.totp_enabled(),
        "backup_remaining": auth.unused_backup_count(),
    }


@app.put("/api/settings")
def put_settings(body: SettingsBody, request: Request):
    require_login(request)
    if body.panel_url:
        url = body.panel_url.strip().rstrip("/")
        if not url.startswith("https://"):
            raise HTTPException(status_code=400, detail="面板地址必须是 https://")
        db.set_setting("panel_url", url)
    if body.origin_ip is not None:
        ip = body.origin_ip.strip()
        if ip:
            db.set_setting("origin_ip", ip)
    if body.admin_password:
        if len(body.admin_password) < 10:
            raise HTTPException(status_code=400, detail="新密码至少 10 位")
        salt = db.get_setting("admin_salt")
        stored = db.get_setting("admin_hash")
        if not body.current_password or not hmac_ok(auth.hash_password(body.current_password, salt), stored):
            raise HTTPException(status_code=400, detail="当前密码不正确")
        secret = db.get_setting("totp_secret")
        if not secret or not body.totp_code or not auth.verify_totp(secret, body.totp_code):
            raise HTTPException(status_code=400, detail="两步验证码不正确")
        new_salt = secrets.token_hex(16)
        db.set_setting("admin_salt", new_salt)
        db.set_setting("admin_hash", auth.hash_password(body.admin_password, new_salt))
    return get_settings(request)


@app.post("/api/2fa/backup-codes")
def regen_backup(body: TotpCodeBody, request: Request):
    require_login(request)
    secret = db.get_setting("totp_secret")
    if not secret or not auth.verify_totp(secret, body.code):
        raise HTTPException(status_code=400, detail="两步验证码不正确")
    codes = auth.generate_backup_codes()
    return {"backup_codes": codes}


@app.get("/api/nodes")
def api_list_nodes(request: Request):
    require_login(request)
    return [enrich(n) for n in db.list_nodes()]


@app.post("/api/nodes")
def api_create_node(body: NodeCreate, request: Request):
    require_login(request)
    remark = body.remark.strip()
    sni = (body.sni or "www.amd.com").strip() or "www.amd.com"
    client_address = (body.client_address or "").strip()
    token = secrets.token_urlsafe(24)
    node = db.create_node(remark, body.node_port, sni, client_address, token)
    db.add_log(node["id"], "节点已创建，等待在目标服务器执行安装命令")
    return enrich(node)


@app.patch("/api/nodes/{node_id}")
def api_patch_node(node_id: int, body: NodePatch, request: Request):
    require_login(request)
    node = db.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    fields = {}
    if body.remark is not None:
        fields["remark"] = body.remark.strip()
    if body.sni is not None:
        fields["sni"] = body.sni.strip() or "www.amd.com"
    if body.client_address is not None:
        fields["client_address"] = body.client_address.strip()
    if body.share_link is not None:
        fields["share_link"] = body.share_link.strip()
        if fields["share_link"]:
            fields["status"] = "online"
            fields["last_error"] = ""
    updated = db.update_node(node_id, **fields)
    return enrich(updated)


@app.delete("/api/nodes/{node_id}")
def api_delete_node(node_id: int, request: Request, mode: str = "panel"):
    require_login(request)
    node = db.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    if agent.deletion_blocked(node):
        raise HTTPException(status_code=409, detail="部署中无法删除，请等待任务结束")
    return agent.request_delete(node)


@app.get("/api/nodes/{node_id}/logs")
def api_node_logs(node_id: int, request: Request):
    require_login(request)
    if not db.get_node(node_id):
        raise HTTPException(status_code=404, detail="节点不存在")
    return db.list_logs(node_id)


@app.get("/b/{token}.sh")
def bootstrap_script(token: str):
    node = db.get_node_by_token(token)
    if not node:
        raise HTTPException(status_code=404, detail="安装令牌无效或已删除")
    if agent.row(node["id"]):
        raise HTTPException(status_code=409, detail="此节点已由主动上报脚本管理，请在面板生成接入命令")
    script = render_script(
        INSTALL_SH,
        public_url(),
        node["token"],
        node["node_port"],
        node["remark"],
        node["sni"],
        node["client_address"],
        origin_ip=origin_ip(),
        panel_host=panel_host(),
    )
    db.update_node(node["id"], status="installing", last_error="")
    db.add_log(node["id"], "目标机已拉取安装脚本")
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8")


@app.get("/u/{token}.sh")
def uninstall_script(token: str):
    node = db.get_node_by_token(token)
    if not node:
        raise HTTPException(status_code=404, detail="卸载令牌无效或已删除")
    if agent.row(node["id"]):
        raise HTTPException(status_code=409, detail="此节点已由主动上报脚本管理")
    script = render_script(
        UNINSTALL_SH,
        public_url(),
        node["token"],
        node["node_port"],
        node["remark"],
        node["sni"],
        node["client_address"],
        origin_ip=origin_ip(),
        panel_host=panel_host(),
    )
    db.update_node(node["id"], status="uninstalling")
    db.add_log(node["id"], "目标机已拉取卸载脚本")
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8")


def agent_node(x_node_token: str | None) -> dict:
    if not x_node_token:
        raise HTTPException(status_code=401, detail="missing token")
    node = db.get_node_by_token(x_node_token)
    if not node:
        raise HTTPException(status_code=401, detail="invalid token")
    if agent.row(node["id"]):
        raise HTTPException(status_code=409, detail="请使用主动上报接口")
    return node


@app.post("/a/hello")
async def agent_hello(request: Request, x_node_token: str | None = Header(default=None)):
    node = agent_node(x_node_token)
    payload = await request.json()
    db.update_node(
        node["id"],
        status="installing",
        host_ip=str(payload.get("ip") or node["host_ip"]),
        hostname=str(payload.get("hostname") or ""),
        os_info=str(payload.get("os") or ""),
        last_seen=db.utcnow(),
        last_error="",
    )
    db.add_log(node["id"], f"服务器上线: {payload.get('hostname')} {payload.get('ip')} {payload.get('os')}")
    return {"ok": True}


@app.post("/a/log")
async def agent_log(request: Request, x_node_token: str | None = Header(default=None)):
    node = agent_node(x_node_token)
    body = (await request.body()).decode("utf-8", "replace").strip()
    if body:
        db.add_log(node["id"], body)
        db.update_node(node["id"], last_seen=db.utcnow())
    return {"ok": True}


@app.post("/a/result")
async def agent_result(request: Request, x_node_token: str | None = Header(default=None)):
    node = agent_node(x_node_token)
    payload = await request.json()
    if payload.get("ok"):
        db.update_node(
            node["id"],
            status="online",
            share_link=str(payload.get("share_link") or ""),
            host_ip=str(payload.get("ip") or node["host_ip"]),
            hostname=str(payload.get("hostname") or node["hostname"]),
            os_info=str(payload.get("os") or node["os_info"]),
            uuid=str(payload.get("uuid") or ""),
            public_key=str(payload.get("public_key") or ""),
            short_id=str(payload.get("short_id") or ""),
            last_error="",
            last_seen=db.utcnow(),
        )
        db.add_log(node["id"], "安装成功，已回传分享链接")
    else:
        err = str(payload.get("error") or "安装失败")
        db.update_node(node["id"], status="error", last_error=err, last_seen=db.utcnow())
        db.add_log(node["id"], f"安装失败: {err}")
    return {"ok": True}


@app.post("/a/heartbeat")
async def agent_heartbeat(x_node_token: str | None = Header(default=None)):
    node = agent_node(x_node_token)
    fields = {"last_seen": db.utcnow()}
    if node["status"] in ("pending", "installing", "error"):
        pass
    elif node["status"] != "uninstalling":
        fields["status"] = "online"
    db.update_node(node["id"], **fields)
    return {"ok": True}


@app.post("/a/gone")
async def agent_gone(x_node_token: str | None = Header(default=None)):
    node = agent_node(x_node_token)
    db.add_log(node["id"], "远端已确认卸载")
    db.update_node(node["id"], status="removed", share_link="", last_seen=db.utcnow())
    return {"ok": True}


@app.get("/sub/{sub_token}")
def subscription(sub_token: str):
    if not sub_token or sub_token != db.get_setting("sub_token"):
        raise HTTPException(status_code=404, detail="not found")
    links = [n["share_link"] for n in db.list_nodes() if n.get("share_link") and n.get("status") == "online"]
    body = "\n".join(links) + ("\n" if links else "")
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/b/") or request.url.path.startswith("/u/"):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


deploy.register(app, require_login)
agent.register(app, require_login, public_url)

@app.on_event("shutdown")
def stop_agent_probes():
    agent.STOP.set()


from fastapi.exceptions import RequestValidationError
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    # Never echo SSH credentials from invalid request bodies.
    return JSONResponse(status_code=422, content={"detail": "; ".join(".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors())})
