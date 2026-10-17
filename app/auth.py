import hashlib
import hmac
import io
import secrets
import time
from datetime import datetime, timedelta, timezone

import pyotp
import segno

from . import db

ISSUER = "singbox-mgmt"
TOTP_WINDOW = 1
CHALLENGE_TTL_SEC = 5 * 60
LOCK_WINDOW_MIN = 15
LOCK_MAX_FAILS = 5
BACKUP_CODE_COUNT = 8


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def hash_backup_code(code: str) -> str:
    normalized = normalize_backup(code)
    return hashlib.sha256(f"sbmgmt-backup:{normalized}".encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "") if ch.isalnum()).upper()


def normalize_backup(code: str) -> str:
    return normalize_code(code)


def client_ip(request) -> str:
    return (request.client.host if request.client else "") or ""


def is_locked(ip: str, username: str) -> bool:
    cutoff = _iso(_utcnow() - timedelta(minutes=LOCK_WINDOW_MIN))
    row = db.get_conn().execute(
        """
        SELECT COUNT(*) AS n FROM login_attempts
        WHERE ok = 0 AND created_at >= ? AND (ip = ? OR username = ?)
        """,
        (cutoff, ip, username),
    ).fetchone()
    return bool(row and row["n"] >= LOCK_MAX_FAILS)


def record_attempt(ip: str, username: str, ok: bool) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO login_attempts(ip, username, ok, created_at) VALUES (?, ?, ?, ?)",
        (ip, username, 1 if ok else 0, db.utcnow()),
    )
    conn.execute(
        """
        DELETE FROM login_attempts WHERE created_at < ?
        """,
        (_iso(_utcnow() - timedelta(days=2)),),
    )
    conn.commit()


def totp_enabled() -> bool:
    return bool(db.get_setting("totp_secret"))


def new_totp_secret() -> str:
    return pyotp.random_base32()


def otpauth_url(username: str, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def qr_data_url(payload: str) -> str:
    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="png", scale=6, border=2)
    import base64

    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def totp_counter_for(code: str, secret: str) -> int | None:
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    got = str(code).strip().replace(" ", "")
    if len(got) != 6 or not got.isdigit():
        return None
    for drift in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        ts = now + drift * 30
        if hmac.compare_digest(totp.at(ts), got):
            return ts // 30
    return None


def verify_totp(secret: str, code: str) -> bool:
    counter = totp_counter_for(code, secret)
    if counter is None:
        return False
    last = db.get_setting("totp_last_counter")
    if last and last.isdigit() and int(last) == counter:
        return False
    db.set_setting("totp_last_counter", str(counter))
    return True


def generate_backup_codes() -> list[str]:
    codes = []
    raw = []
    for _ in range(BACKUP_CODE_COUNT):
        token = secrets.token_hex(4).upper()
        pretty = f"{token[:4]}-{token[4:]}"
        codes.append(pretty)
        raw.append(hash_backup_code(pretty))
    conn = db.get_conn()
    conn.execute("DELETE FROM backup_codes")
    conn.executemany(
        "INSERT INTO backup_codes(code_hash, used_at) VALUES (?, '')",
        [(h,) for h in raw],
    )
    conn.commit()
    return codes


def verify_backup_code(code: str) -> bool:
    digest = hash_backup_code(code)
    conn = db.get_conn()
    row = conn.execute(
        "SELECT id, used_at FROM backup_codes WHERE code_hash = ?",
        (digest,),
    ).fetchone()
    if not row or row["used_at"]:
        return False
    conn.execute(
        "UPDATE backup_codes SET used_at = ? WHERE id = ?",
        (db.utcnow(), row["id"]),
    )
    conn.commit()
    return True


def unused_backup_count() -> int:
    row = db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM backup_codes WHERE used_at = ''"
    ).fetchone()
    return int(row["n"] if row else 0)


def create_challenge(kind: str, ip: str, extra: str = "") -> str:
    token = secrets.token_urlsafe(32)
    expires = _iso(_utcnow() + timedelta(seconds=CHALLENGE_TTL_SEC))
    conn = db.get_conn()
    conn.execute(
        "DELETE FROM auth_challenges WHERE expires_at < ?",
        (db.utcnow(),),
    )
    conn.execute(
        """
        INSERT INTO auth_challenges(token, kind, extra, ip, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (token, kind, extra, ip, db.utcnow(), expires),
    )
    conn.commit()
    return token


def get_challenge(token: str, kind: str) -> dict | None:
    if not token:
        return None
    conn = db.get_conn()
    row = conn.execute(
        "SELECT * FROM auth_challenges WHERE token = ?",
        (token,),
    ).fetchone()
    if not row:
        return None
    if row["kind"] != kind:
        return None
    if parse_iso(row["expires_at"]) < _utcnow():
        conn.execute("DELETE FROM auth_challenges WHERE token = ?", (token,))
        conn.commit()
        return None
    return dict(row)


def consume_challenge(token: str) -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM auth_challenges WHERE token = ?", (token,))
    conn.commit()


def take_challenge(token: str, kind: str, ip: str) -> dict | None:
    row = get_challenge(token, kind)
    if not row:
        return None
    consume_challenge(token)
    return row


def establish_session(request, username: str) -> None:
    request.session.clear()
    request.session["user"] = username
    request.session["authed"] = True
    request.session["login_at"] = db.utcnow()
    request.session["ip"] = client_ip(request)
