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
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    parent_id INTEGER DEFAULT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES groups(id) ON DELETE SET NULL
);

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
    group_id INTEGER DEFAULT 1,
    group_name TEXT NOT NULL DEFAULT 'Root',
    created_at TEXT NOT NULL,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
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

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

        try:
            conn.execute("ALTER TABLE targets ADD COLUMN group_id INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE targets ADD COLUMN group_name TEXT DEFAULT 'Root'")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE groups ADD COLUMN parent_id INTEGER DEFAULT NULL")
        except sqlite3.OperationalError:
            pass

        # Ensure default Root group exists
        root_group = conn.execute("SELECT id, name FROM groups WHERE LOWER(name) = 'root'").fetchone()
        if not root_group:
            conn.execute(
                "INSERT INTO groups (name, description, parent_id, created_at) VALUES (?, ?, NULL, ?)",
                ("Root", "Default root group", _now_iso())
            )
            root_group = conn.execute("SELECT id, name FROM groups WHERE LOWER(name) = 'root'").fetchone()
        
        root_id = root_group["id"]
        root_name = root_group["name"]
        conn.execute(
            "UPDATE targets SET group_id = ?, group_name = ? WHERE group_id IS NULL OR group_name IS NULL",
            (root_id, root_name)
        )


# --- Groups ------------------------------------------------------------

def _build_group_hierarchy(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not groups:
        return []
    
    by_id = {g["id"]: g for g in groups}
    children_map: dict[Optional[int], list[int]] = {}
    root_ids: list[int] = []

    for g in groups:
        pid = g.get("parent_id")
        if pid and pid in by_id:
            g["parent_name"] = by_id[pid]["name"]
            children_map.setdefault(pid, []).append(g["id"])
        else:
            g["parent_id"] = None
            g["parent_name"] = None
            root_ids.append(g["id"])

    def sort_ids(g_ids: list[int]) -> list[int]:
        def key_fn(gid: int) -> tuple[int, str]:
            name = (by_id[gid]["name"] or "").lower()
            return (0 if name == "root" else 1, name)
        return sorted(g_ids, key=key_fn)

    ordered: list[dict[str, Any]] = []
    visited: set[int] = set()

    def traverse(gid: int, current_path: list[str], depth: int) -> None:
        if gid in visited:
            return
        visited.add(gid)
        g = by_id[gid]
        new_path = current_path + [g["name"]]
        g["full_path"] = " / ".join(new_path)
        g["level"] = depth
        ordered.append(g)
        for child_id in sort_ids(children_map.get(gid, [])):
            traverse(child_id, new_path, depth + 1)

    for rid in sort_ids(root_ids):
        traverse(rid, [], 0)

    for g in groups:
        if g["id"] not in visited:
            g["full_path"] = g["name"]
            g["level"] = 0
            ordered.append(g)

    return ordered


def create_group(name: str, description: Optional[str] = None, parent_id: Optional[int] = None) -> dict[str, Any]:
    with get_conn() as conn:
        # Validate parent_id if given
        valid_pid = None
        if parent_id is not None:
            parent = conn.execute("SELECT id FROM groups WHERE id = ?", (parent_id,)).fetchone()
            if parent:
                valid_pid = parent["id"]
        
        cur = conn.execute(
            "INSERT INTO groups (name, description, parent_id, created_at) VALUES (?, ?, ?, ?)",
            (name.strip(), description.strip() if description else None, valid_pid, _now_iso())
        )
        group_id = cur.lastrowid
    return get_group(group_id) or {}


def list_groups() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT g.*, COUNT(t.id) AS target_count "
            "FROM groups g "
            "LEFT JOIN targets t ON (t.group_id = g.id OR (t.group_id IS NULL AND LOWER(g.name) = 'root')) "
            "GROUP BY g.id "
            "ORDER BY CASE WHEN LOWER(g.name) = 'root' THEN 0 ELSE 1 END, g.name ASC"
        ).fetchall()
        groups = [dict(r) for r in rows]
        return _build_group_hierarchy(groups)


def get_group(group_id: int) -> Optional[dict[str, Any]]:
    groups = list_groups()
    for g in groups:
        if g["id"] == group_id:
            return g
    return None


def get_group_by_name(name: str) -> Optional[dict[str, Any]]:
    groups = list_groups()
    for g in groups:
        if g["name"].lower() == name.strip().lower():
            return g
    return None


def update_group(group_id: int, **fields: Any) -> Optional[dict[str, Any]]:
    if not fields:
        return get_group(group_id)
    allowed = {"name", "description", "parent_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_group(group_id)
    if "name" in updates and updates["name"]:
        updates["name"] = updates["name"].strip()
    if "description" in updates and updates["description"]:
        updates["description"] = updates["description"].strip()
    
    with get_conn() as conn:
        group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not group:
            return None
        
        if "parent_id" in updates:
            pid = updates["parent_id"]
            if pid is not None:
                if pid == group_id:
                    raise ValueError("A group cannot be its own parent")
                parent = conn.execute("SELECT id FROM groups WHERE id = ?", (pid,)).fetchone()
                if not parent:
                    updates["parent_id"] = None
                elif group["name"].lower() == "root":
                    updates["parent_id"] = None

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [group_id]
        conn.execute(f"UPDATE groups SET {set_clause} WHERE id = ?", values)
        if "name" in updates and updates["name"]:
            conn.execute("UPDATE targets SET group_name = ? WHERE group_id = ?", (updates["name"], group_id))
    
    return get_group(group_id)


def delete_group(group_id: int) -> bool:
    with get_conn() as conn:
        group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not group or group["name"].lower() == "root":
            return False
        
        root_group = conn.execute("SELECT id, name FROM groups WHERE LOWER(name) = 'root'").fetchone()
        root_id = root_group["id"] if root_group else 1
        root_name = root_group["name"] if root_group else "Root"
        parent_id = group["parent_id"] or root_id

        # Reassign child groups to the parent group (or Root)
        conn.execute("UPDATE groups SET parent_id = ? WHERE parent_id = ?", (parent_id, group_id))
        
        # Reassign targets to Root group
        conn.execute("UPDATE targets SET group_id = ?, group_name = ? WHERE group_id = ?", (root_id, root_name, group_id))
        cur = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        return cur.rowcount > 0


# --- Targets -----------------------------------------------------------

def _resolve_target_group(conn: sqlite3.Connection, group_id: Optional[int] = None, group_name: Optional[str] = None) -> tuple[int, str]:
    if group_id is not None:
        grow = conn.execute("SELECT id, name FROM groups WHERE id = ?", (group_id,)).fetchone()
        if grow:
            return grow["id"], grow["name"]
    if group_name is not None and group_name.strip():
        grow = conn.execute("SELECT id, name FROM groups WHERE LOWER(name) = LOWER(?)", (group_name.strip(),)).fetchone()
        if grow:
            return grow["id"], grow["name"]
        else:
            cur_g = conn.execute("INSERT INTO groups (name, description, created_at) VALUES (?, NULL, ?)", (group_name.strip(), _now_iso()))
            return cur_g.lastrowid, group_name.strip()
    
    root = conn.execute("SELECT id, name FROM groups WHERE LOWER(name) = 'root'").fetchone()
    if root:
        return root["id"], root["name"]
    cur_g = conn.execute("INSERT INTO groups (name, description, created_at) VALUES ('Root', 'Default root group', ?)", (_now_iso(),))
    return cur_g.lastrowid, "Root"


def create_target(
    name: str, host: str, interval: int, count: int, timeout: float, enabled: bool,
    telegram_enabled: bool = False, telegram_name: Optional[str] = None,
    telegram_token: Optional[str] = None, telegram_chat_id: Optional[str] = None,
    telegram_notify_down: bool = True, telegram_notify_up: bool = True,
    telegram_custom: bool = False, telegram_template_down: Optional[str] = None,
    telegram_template_up: Optional[str] = None,
    group_id: Optional[int] = None,
    group_name: Optional[str] = None,
) -> dict[str, Any]:
    with get_conn() as conn:
        resolved_group_id, resolved_group_name = _resolve_target_group(conn, group_id, group_name)
        cur = conn.execute(
            "INSERT INTO targets (name, host, interval, count, timeout, enabled, "
            "telegram_enabled, telegram_name, telegram_token, telegram_chat_id, "
            "telegram_notify_down, telegram_notify_up, telegram_custom, "
            "telegram_template_down, telegram_template_up, group_id, group_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, host, interval, count, timeout, int(enabled),
             int(telegram_enabled), telegram_name, telegram_token, telegram_chat_id,
             int(telegram_notify_down), int(telegram_notify_up), int(telegram_custom),
             telegram_template_down, telegram_template_up, resolved_group_id, resolved_group_name, _now_iso()),
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
               "telegram_custom", "telegram_template_down", "telegram_template_up",
               "group_id", "group_name"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    
    # Allow explicitly setting optional fields to None/False
    for k in allowed:
        if k in fields and fields[k] is None:
            updates[k] = None

    if not updates:
        return get_target(target_id)

    with get_conn() as conn:
        if "group_id" in updates or "group_name" in updates:
            gid, gname = _resolve_target_group(conn, updates.get("group_id"), updates.get("group_name"))
            updates["group_id"] = gid
            updates["group_name"] = gname

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


# --- System Settings ---------------------------------------------------

DEFAULT_SETTINGS: dict[str, str] = {
    "site_name": "PingOn",
    "site_tagline": "Keep Your Network On.",
    "logo_url": "",
    "favicon_url": "",
    "use_logo": "1",
}


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row["value"]
        return default if default is not None else DEFAULT_SETTINGS.get(key)


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )


def get_all_settings() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
        settings = dict(DEFAULT_SETTINGS)
        for r in rows:
            settings[r["key"]] = r["value"]
        
        return {
            "site_name": settings.get("site_name", "PingOn"),
            "site_tagline": settings.get("site_tagline", "Keep Your Network On."),
            "logo_url": settings.get("logo_url") or None,
            "favicon_url": settings.get("favicon_url") or None,
            "use_logo": settings.get("use_logo", "1") in ("1", "true", "True", True),
            "updated_at": settings.get("updated_at", _now_iso()),
        }


def update_settings(updates: dict[str, Any]) -> dict[str, Any]:
    with get_conn() as conn:
        for k, v in updates.items():
            if k in DEFAULT_SETTINGS or k == "updated_at":
                str_val = str(int(v)) if isinstance(v, bool) else ("" if v is None else str(v))
                conn.execute(
                    "INSERT INTO system_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, str_val)
                )
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('updated_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_now_iso(),)
        )
    return get_all_settings()


def reset_settings() -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute("DELETE FROM system_settings")
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT INTO system_settings (key, value) VALUES (?, ?)", (k, v))
        conn.execute("INSERT INTO system_settings (key, value) VALUES ('updated_at', ?)", (_now_iso(),))
    return get_all_settings()
