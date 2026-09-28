"""SQLite persistence layer.

Design notes:
- Plain `sqlite3` (not an ORM) is used deliberately — this project's
  data model is small (2 tables) and an ORM would add indirection
  without real benefit.
- Every function opens its own short-lived connection. sqlite3
  connections are not safe to share across threads by default, and
  this app calls into the DB both from the asyncio event loop thread
  (via `asyncio.to_thread`) and, historically, could be called from
  worker threads — per-call connections sidestep that entirely at a
  small, acceptable cost given SQLite's fast connection setup and WAL
  mode below.
- WAL mode is enabled so background monitoring writes don't block API
  reads (and vice versa).
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

DB_PATH = Path(__file__).resolve().parent / "data" / "ping_monitor.db"
MAX_HISTORY_PER_TARGET = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    interval INTEGER NOT NULL DEFAULT 10,
    count INTEGER NOT NULL DEFAULT 4,
    timeout REAL NOT NULL DEFAULT 1.0,
    enabled INTEGER NOT NULL DEFAULT 1,
    telegram_enabled INTEGER NOT NULL DEFAULT 0,
    telegram_name TEXT,
    telegram_token TEXT,
    telegram_chat_id TEXT,
    telegram_notify_down INTEGER NOT NULL DEFAULT 1,
    telegram_notify_up INTEGER NOT NULL DEFAULT 1,
    telegram_custom INTEGER NOT NULL DEFAULT 0,
    telegram_template_down TEXT,
    telegram_template_up TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitoring_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_status TEXT NOT NULL,
    latency_min REAL,
    latency_avg REAL,
    latency_max REAL,
    packet_loss REAL NOT NULL,
    sent INTEGER NOT NULL,
    received INTEGER NOT NULL,
    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_results_target_ts
    ON monitoring_results(target_id, timestamp);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'guest',
    is_active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        try:
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_name TEXT")
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_token TEXT")
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_chat_id TEXT")
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_notify_down INTEGER NOT NULL DEFAULT 1")
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_notify_up INTEGER NOT NULL DEFAULT 1")
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_custom INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_template_down TEXT")
            conn.execute("ALTER TABLE targets ADD COLUMN telegram_template_up TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass


# --- Targets -----------------------------------------------------------

def create_target(
    name: str, host: str, interval: int, count: int, timeout: float, enabled: bool,
    telegram_enabled: bool = False, telegram_name: Optional[str] = None,
    telegram_token: Optional[str] = None, telegram_chat_id: Optional[str] = None,
    telegram_notify_down: bool = True, telegram_notify_up: bool = True,
    telegram_custom: bool = False, telegram_template_down: Optional[str] = None,
    telegram_template_up: Optional[str] = None
) -> dict[str, Any]:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO targets (name, host, interval, count, timeout, enabled, "
            "telegram_enabled, telegram_name, telegram_token, telegram_chat_id, "
            "telegram_notify_down, telegram_notify_up, telegram_custom, "
            "telegram_template_down, telegram_template_up, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, host, interval, count, timeout, int(enabled),
             int(telegram_enabled), telegram_name, telegram_token, telegram_chat_id,
             int(telegram_notify_down), int(telegram_notify_up), int(telegram_custom),
             telegram_template_down, telegram_template_up, _now_iso()),
        )
        target_id = cur.lastrowid
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return dict(row)


def list_targets() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def get_target(target_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return dict(row) if row else None


def update_target(target_id: int, **fields: Any) -> Optional[dict[str, Any]]:
    if not fields:
        return get_target(target_id)

    allowed = {"name", "host", "interval", "count", "timeout", "enabled", 
               "telegram_enabled", "telegram_name", "telegram_token", 
               "telegram_chat_id", "telegram_notify_down", "telegram_notify_up", 
               "telegram_custom", "telegram_template_down", "telegram_template_up"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    
    # Allow explicitly setting optional fields to None/False
    for k in allowed:
        if k in fields and fields[k] is None:
            updates[k] = None

    if not updates:
        return get_target(target_id)

    if "enabled" in updates and updates["enabled"] is not None:
        updates["enabled"] = int(updates["enabled"])
    if "telegram_enabled" in updates and updates["telegram_enabled"] is not None:
        updates["telegram_enabled"] = int(updates["telegram_enabled"])
    if "telegram_notify_down" in updates and updates["telegram_notify_down"] is not None:
        updates["telegram_notify_down"] = int(updates["telegram_notify_down"])
    if "telegram_notify_up" in updates and updates["telegram_notify_up"] is not None:
        updates["telegram_notify_up"] = int(updates["telegram_notify_up"])
    if "telegram_custom" in updates and updates["telegram_custom"] is not None:
        updates["telegram_custom"] = int(updates["telegram_custom"])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [target_id]

    with get_conn() as conn:
        conn.execute(f"UPDATE targets SET {set_clause} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return dict(row) if row else None


def delete_target(target_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        return cur.rowcount > 0


# --- Monitoring results / history --------------------------------------

def insert_result(
    target_id: int,
    timestamp: str,
    status: str,
    raw_status: str,
    latency_min: Optional[float],
    latency_avg: Optional[float],
    latency_max: Optional[float],
    packet_loss: float,
    sent: int,
    received: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO monitoring_results "
            "(target_id, timestamp, status, raw_status, latency_min, latency_avg, "
            "latency_max, packet_loss, sent, received) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                target_id,
                timestamp,
                status,
                raw_status,
                latency_min,
                latency_avg,
                latency_max,
                packet_loss,
                sent,
                received,
            ),
        )
        # Cheap bounded cleanup: only runs the DELETE when the target is
        # actually over the cap, so steady-state writes stay a single
        # INSERT. Keeps the table from growing unbounded per section 33.
        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM monitoring_results WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        if count_row["c"] > MAX_HISTORY_PER_TARGET:
            excess = count_row["c"] - MAX_HISTORY_PER_TARGET
            conn.execute(
                "DELETE FROM monitoring_results WHERE id IN ("
                "  SELECT id FROM monitoring_results WHERE target_id = ? "
                "  ORDER BY id ASC LIMIT ?)",
                (target_id, excess),
            )


def get_latest_result(target_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_results WHERE target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (target_id,),
        ).fetchone()
        return dict(row) if row else None


def get_history(target_id: int, hours: int = 24, limit: int = 20_000) -> list[dict[str, Any]]:
    cutoff = _now_iso(offset_hours=-hours)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT timestamp, status, latency_avg, packet_loss FROM monitoring_results "
            "WHERE target_id = ? AND timestamp >= ? "
            "ORDER BY timestamp ASC LIMIT ?",
            (target_id, cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def _now_iso(offset_hours: float = 0.0) -> str:
    import datetime

    dt = datetime.datetime.now(datetime.timezone.utc).astimezone() + datetime.timedelta(
        hours=offset_hours
    )
    return dt.isoformat()


# --- Users -----------------------------------------------------------

def create_user(
    username: str, 
    password_hash: str, 
    role: str = "guest", 
    is_active: bool = True,
    must_change_password: bool = False
) -> dict[str, Any]:
    with get_conn() as conn:
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, password_hash, role, int(is_active), int(must_change_password), now, now),
        )
        user_id = cur.lastrowid
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row)


def get_user(user_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def update_user(user_id: int, **fields: Any) -> Optional[dict[str, Any]]:
    if not fields:
        return get_user(user_id)

    allowed = {"username", "password_hash", "role", "is_active", "must_change_password"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    
    if not updates:
        return get_user(user_id)

    if "is_active" in updates:
        updates["is_active"] = int(updates["is_active"])
    if "must_change_password" in updates:
        updates["must_change_password"] = int(updates["must_change_password"])

    updates["updated_at"] = _now_iso()
    
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]

    with get_conn() as conn:
        conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def delete_user(user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount > 0
