import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("SINGBOX_MGMT_DATA", "/app/data"))
DB_PATH = DATA_DIR / "app.db"

_local = threading.local()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            remark TEXT NOT NULL,
            node_port INTEGER NOT NULL,
            sni TEXT NOT NULL DEFAULT 'www.amd.com',
            client_address TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            host_ip TEXT NOT NULL DEFAULT '',
            hostname TEXT NOT NULL DEFAULT '',
            os_info TEXT NOT NULL DEFAULT '',
            share_link TEXT NOT NULL DEFAULT '',
            uuid TEXT NOT NULL DEFAULT '',
            public_key TEXT NOT NULL DEFAULT '',
            short_id TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL,
            line TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_logs_node ON logs(node_id, id);
        CREATE INDEX IF NOT EXISTS idx_nodes_token ON nodes(token);

        CREATE TABLE IF NOT EXISTS auth_challenges (
            token TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            extra TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            username TEXT NOT NULL,
            ok INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS backup_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_hash TEXT NOT NULL UNIQUE,
            used_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_attempts_time ON login_attempts(created_at);
        """
    )
    conn.commit()


def get_setting(key: str, default: str = "") -> str:
    row = get_conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def node_from_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def list_nodes() -> list[dict]:
    rows = get_conn().execute("SELECT * FROM nodes ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_node(node_id: int) -> dict | None:
    return node_from_row(
        get_conn().execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    )


def get_node_by_token(token: str) -> dict | None:
    return node_from_row(
        get_conn().execute("SELECT * FROM nodes WHERE token = ?", (token,)).fetchone()
    )


def create_node(remark: str, node_port: int, sni: str, client_address: str, token: str) -> dict:
    now = utcnow()
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO nodes(remark, node_port, sni, client_address, token, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (remark, node_port, sni, client_address, token, now, now),
    )
    conn.commit()
    return get_node(cur.lastrowid)


def update_node(node_id: int, **fields) -> dict | None:
    if not fields:
        return get_node(node_id)
    fields["updated_at"] = utcnow()
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [node_id]
    conn = get_conn()
    conn.execute(f"UPDATE nodes SET {cols} WHERE id = ?", values)
    conn.commit()
    return get_node(node_id)


def delete_node(node_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    conn.commit()


def add_log(node_id: int, line: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO logs(node_id, line, created_at) VALUES (?, ?, ?)",
        (node_id, line[:4000], utcnow()),
    )
    # keep last 300 lines per node
    conn.execute(
        """
        DELETE FROM logs WHERE node_id = ? AND id NOT IN (
            SELECT id FROM logs WHERE node_id = ? ORDER BY id DESC LIMIT 300
        )
        """,
        (node_id, node_id),
    )
    conn.commit()


def list_logs(node_id: int, limit: int = 200) -> list[dict]:
    rows = get_conn().execute(
        "SELECT line, created_at FROM logs WHERE node_id = ? ORDER BY id DESC LIMIT ?",
        (node_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]
