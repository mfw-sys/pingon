"""End-to-end API tests.

Uses a temporary SQLite file (monkeypatched over db.DB_PATH) so tests
never touch the real data/ping_monitor.db. Targets point at 127.0.0.1
so tests don't depend on outbound network access.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_module


from auth.jwt import create_access_token


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    # Import main AFTER patching DB_PATH so init_db() (called in the
    # lifespan handler) writes to the temp file, and BEFORE reuse
    # across tests via a fresh module import each time.
    import importlib

    import main as main_module

    importlib.reload(main_module)

    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as c:
        admin_user = db_module.get_user_by_username("admin")
        if admin_user:
            token = create_access_token(
                data={"sub": str(admin_user["id"]), "username": "admin", "role": "administrator"}
            )
            c.headers["Authorization"] = f"Bearer {token}"
        yield c


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_dashboard_empty(client):
    res = client.get("/api/dashboard")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 0
    assert body["targets"] == []


def test_create_and_list_target(client):
    res = client.post(
        "/api/targets",
        json={"name": "Localhost", "host": "127.0.0.1", "interval": 5, "count": 2, "timeout": 1, "enabled": True},
    )
    assert res.status_code == 201
    target = res.json()
    assert target["name"] == "Localhost"
    assert target["host"] == "127.0.0.1"
    assert target["id"] > 0

    res = client.get("/api/targets")
    assert res.status_code == 200
    assert len(res.json()) == 1


def test_create_target_invalid_host_rejected(client):
    res = client.post(
        "/api/targets",
        json={"name": "Bad", "host": "invalid-host-###", "interval": 5, "count": 2, "timeout": 1, "enabled": True},
    )
    assert res.status_code == 422


def test_create_target_missing_name_rejected(client):
    res = client.post(
        "/api/targets",
        json={"name": "", "host": "127.0.0.1", "interval": 5, "count": 2, "timeout": 1, "enabled": True},
    )
    assert res.status_code == 422


def test_get_update_delete_target(client):
    created = client.post(
        "/api/targets",
        json={"name": "Router", "host": "127.0.0.1", "interval": 5, "count": 2, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]

    res = client.get(f"/api/targets/{tid}")
    assert res.status_code == 200
    assert res.json()["name"] == "Router"

    res = client.put(f"/api/targets/{tid}", json={"name": "Router Updated", "interval": 15})
    assert res.status_code == 200
    assert res.json()["name"] == "Router Updated"
    assert res.json()["interval"] == 15

    res = client.delete(f"/api/targets/{tid}")
    assert res.status_code == 204

    res = client.get(f"/api/targets/{tid}")
    assert res.status_code == 404


def test_get_nonexistent_target_404(client):
    res = client.get("/api/targets/9999")
    assert res.status_code == 404


def test_update_nonexistent_target_404(client):
    res = client.put("/api/targets/9999", json={"name": "X"})
    assert res.status_code == 404


def test_delete_nonexistent_target_404(client):
    res = client.delete("/api/targets/9999")
    assert res.status_code == 404


def test_on_demand_ping_localhost_up(client):
    res = client.post("/api/ping", json={"host": "127.0.0.1", "count": 2, "timeout": 1, "interval": 0.1})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "UP"
    assert body["sent"] == 2
    assert body["received"] == 2
    assert body["packet_loss"] == 0.0
    # RTT must be native ICMP RTT, not inflated by HTTP/process overhead.
    assert body["latency"]["avg"] < 50.0


def test_on_demand_ping_invalid_host_returns_error_status(client):
    res = client.post("/api/ping", json={"host": "invalid-host-###", "count": 2, "timeout": 1, "interval": 0.1})
    assert res.status_code == 200  # not an HTTP error - it's a monitoring outcome
    body = res.json()
    assert body["status"] == "ERROR"
    assert body["error"] is not None


def test_ping_request_validation_rejects_bad_params(client):
    res = client.post("/api/ping", json={"host": "127.0.0.1", "count": 0})
    assert res.status_code == 422

    res = client.post("/api/ping", json={"host": "127.0.0.1", "count": 999})
    assert res.status_code == 422


def test_background_monitoring_produces_history_and_dashboard_state(client):
    created = client.post(
        "/api/targets",
        json={"name": "BG Test", "host": "127.0.0.1", "interval": 1, "count": 2, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]

    # Give the background asyncio loop time for at least one cycle.
    deadline = time.time() + 6
    history = []
    while time.time() < deadline:
        res = client.get(f"/api/targets/{tid}/history?hours=1").json()
        history = res.get("heartbeat", [])
        if history:
            break
        time.sleep(0.5)

    assert history, "background monitoring did not produce any history within timeout"
    assert history[0]["status"] in ("UP", "DEGRADED", "DOWN")

    dash = client.get("/api/dashboard").json()
    matching = [t for t in dash["targets"] if t["id"] == tid]
    assert len(matching) == 1
    assert matching[0]["status"] in ("UP", "DEGRADED", "DOWN")
    assert matching[0]["raw_status"] in ("UP", "DEGRADED", "DOWN")


def test_history_hours_validation(client):
    created = client.post(
        "/api/targets",
        json={"name": "T", "host": "127.0.0.1", "interval": 5, "count": 2, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]

    res = client.get(f"/api/targets/{tid}/history?hours=0")
    assert res.status_code == 422

    res = client.get(f"/api/targets/{tid}/history?hours=9999")
    assert res.status_code == 422


def test_disabling_target_stops_history_growth(client):
    created = client.post(
        "/api/targets",
        json={"name": "Disable Test", "host": "127.0.0.1", "interval": 1, "count": 1, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]

    time.sleep(1.5)  # let at least one cycle happen
    client.put(f"/api/targets/{tid}", json={"enabled": False})

    count_after_disable = len(client.get(f"/api/targets/{tid}/history?hours=1").json()["heartbeat"])
    time.sleep(2.5)
    count_later = len(client.get(f"/api/targets/{tid}/history?hours=1").json()["heartbeat"])

    assert count_later == count_after_disable, "monitoring kept running after target was disabled"


# --------------------------------------------------------------------
# Pause / Resume
# --------------------------------------------------------------------

def test_pause_stops_scheduler_and_marks_paused(client):
    """Test 1 — Pause: UP -> Pause -> PAUSED -> scheduler runs -> target not pinged."""
    created = client.post(
        "/api/targets",
        json={"name": "Pause Test", "host": "127.0.0.1", "interval": 1, "count": 1, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]

    # Let it actually go UP first.
    deadline = time.time() + 6
    while time.time() < deadline:
        dash = client.get("/api/dashboard").json()
        row = next(t for t in dash["targets"] if t["id"] == tid)
        if row["status"] == "UP":
            break
        time.sleep(0.3)
    assert row["status"] == "UP"

    res = client.post(f"/api/targets/{tid}/pause")
    assert res.status_code == 200
    assert res.json() == {"id": tid, "status": "PAUSED"}

    dash = client.get("/api/dashboard").json()
    row = next(t for t in dash["targets"] if t["id"] == tid)
    assert row["status"] == "PAUSED"
    assert row["raw_status"] == "PAUSED"

    history_before = client.get(f"/api/targets/{tid}/history?hours=1").json()["heartbeat"]
    count_before = len(history_before)
    assert history_before[-1]["status"] == "PAUSED"  # pause marker recorded

    # Scheduler tick would have fired at least twice in 3s given interval=1.
    time.sleep(3)
    history_after = client.get(f"/api/targets/{tid}/history?hours=1").json()["heartbeat"]
    assert len(history_after) == count_before, "target was pinged again while PAUSED"

    dash = client.get("/api/dashboard").json()
    row = next(t for t in dash["targets"] if t["id"] == tid)
    assert row["status"] == "PAUSED", "status drifted away from PAUSED with no ping performed"


def test_resume_does_not_assume_up(client):
    """Test 2 — Resume: PAUSED -> Resume -> check -> UP/DOWN from real ping."""
    created = client.post(
        "/api/targets",
        json={"name": "Resume Test", "host": "127.0.0.1", "interval": 2, "count": 1, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]

    client.post(f"/api/targets/{tid}/pause")
    res = client.post(f"/api/targets/{tid}/resume")
    assert res.status_code == 200
    # Resume must not claim UP before any check has actually run.
    assert res.json()["status"] in ("UNKNOWN",)

    deadline = time.time() + 6
    row = None
    while time.time() < deadline:
        dash = client.get("/api/dashboard").json()
        row = next(t for t in dash["targets"] if t["id"] == tid)
        if row["status"] in ("UP", "DOWN", "DEGRADED"):
            break
        time.sleep(0.3)
    assert row["status"] == "UP"  # 127.0.0.1 really is reachable


def test_pause_while_down_stays_paused_not_up(client):
    """Test 3 — Pause while DOWN must become PAUSED, never UP."""
    created = client.post(
        "/api/targets",
        json={"name": "Pause While Down", "host": "203.0.113.55", "interval": 1, "count": 1, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]

    deadline = time.time() + 6
    row = None
    while time.time() < deadline:
        dash = client.get("/api/dashboard").json()
        row = next(t for t in dash["targets"] if t["id"] == tid)
        if row["status"] == "DOWN":
            break
        time.sleep(0.3)
    assert row["status"] == "DOWN"

    client.post(f"/api/targets/{tid}/pause")
    dash = client.get("/api/dashboard").json()
    row = next(t for t in dash["targets"] if t["id"] == tid)
    assert row["status"] == "PAUSED"
    assert row["status"] != "UP"


def test_pause_produces_no_status_changed_notification(client):
    """Test 4 — Paused targets must never report status_changed=True
    (i.e. never fire a DOWN/UP toast) while skipped by the scheduler."""
    created = client.post(
        "/api/targets",
        json={"name": "No Notify Test", "host": "127.0.0.1", "interval": 1, "count": 1, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]
    time.sleep(2)

    client.post(f"/api/targets/{tid}/pause")
    time.sleep(3)

    dash = client.get("/api/dashboard").json()
    row = next(t for t in dash["targets"] if t["id"] == tid)
    assert row["status_changed"] is False


def test_pause_idempotent_and_resume_idempotent(client):
    created = client.post(
        "/api/targets",
        json={"name": "Idempotent Test", "host": "127.0.0.1", "interval": 5, "count": 1, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]

    r1 = client.post(f"/api/targets/{tid}/pause")
    r2 = client.post(f"/api/targets/{tid}/pause")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["status"] == r2.json()["status"] == "PAUSED"

    client.post(f"/api/targets/{tid}/resume")
    r3 = client.post(f"/api/targets/{tid}/resume")
    assert r3.status_code == 200


def test_pause_nonexistent_target_404(client):
    res = client.post("/api/targets/9999/pause")
    assert res.status_code == 404

    res = client.post("/api/targets/9999/resume")
    assert res.status_code == 404


def test_pause_preserves_existing_history(client):
    """History from before the pause must not be deleted."""
    created = client.post(
        "/api/targets",
        json={"name": "History Preserve", "host": "127.0.0.1", "interval": 1, "count": 1, "timeout": 1, "enabled": True},
    ).json()
    tid = created["id"]
    time.sleep(2.5)

    history_before_pause = client.get(f"/api/targets/{tid}/history?hours=1").json()["heartbeat"]
    assert len(history_before_pause) >= 1
    pre_pause_timestamps = {h["timestamp"] for h in history_before_pause}

    client.post(f"/api/targets/{tid}/pause")

    history_after_pause = client.get(f"/api/targets/{tid}/history?hours=1").json()["heartbeat"]
    after_timestamps = {h["timestamp"] for h in history_after_pause}
    assert pre_pause_timestamps.issubset(after_timestamps), "pre-pause history rows were lost"


# --------------------------------------------------------------------
# Telegram Notification Tests
# --------------------------------------------------------------------

def test_create_target_with_telegram_config(client):
    res = client.post(
        "/api/targets",
        json={
            "name": "TG Target",
            "host": "127.0.0.1",
            "interval": 10,
            "count": 2,
            "timeout": 1,
            "enabled": True,
            "telegram_enabled": True,
            "telegram_name": "NOC Telegram",
            "telegram_token": "123456789:AAxxxxxxxxxxxxxxxx",
            "telegram_chat_id": "-1001234567890",
            "telegram_notify_down": True,
            "telegram_notify_up": True,
            "telegram_custom": False,
        },
    )
    assert res.status_code == 201
    target = res.json()
    assert target["telegram_enabled"] is True
    assert target["telegram_name"] == "NOC Telegram"
    assert target["telegram_chat_id"] == "-1001234567890"
    # Token must be masked in response
    assert "****" in target["telegram_token"]


def test_update_target_preserves_masked_token(client):
    created = client.post(
        "/api/targets",
        json={
            "name": "Mask Test",
            "host": "127.0.0.1",
            "telegram_enabled": True,
            "telegram_token": "my-secret-bot-token-12345",
            "telegram_chat_id": "12345",
        },
    ).json()
    tid = created["id"]
    masked_token = created["telegram_token"]
    assert "****" in masked_token

    # Update name with masked token sent back
    updated = client.put(
        f"/api/targets/{tid}",
        json={"name": "Mask Test Updated", "telegram_token": masked_token},
    ).json()
    assert updated["name"] == "Mask Test Updated"

    # DB still has original unmasked token
    db_target = db_module.get_target(tid)
    assert db_target["telegram_token"] == "my-secret-bot-token-12345"


def test_test_telegram_endpoint(client, monkeypatch):
    from services.telegram_service import telegram_service

    sent = []

    async def mock_send(token, chat_id, text):
        sent.append((token, chat_id, text))
        return True

    monkeypatch.setattr(telegram_service, "send_message", mock_send)

    res = client.post(
        "/api/targets/test-telegram",
        json={"token": "123:ABC", "chat_id": "999", "name": "Test Config"},
    )
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert len(sent) == 1
    assert sent[0][0] == "123:ABC"
    assert sent[0][1] == "999"
    assert "PingOn Test Notification" in sent[0][2]


# --------------------------------------------------------------------
# JWT & Force Change Password Tests
# --------------------------------------------------------------------

def test_force_change_password_on_default_admin(client):
    # 1. Login with default admin123 credentials
    res = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["must_change_password"] is True
    
    # 2. Check /api/auth/me also flags must_change_password
    token = body["access_token"]
    res_me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res_me.status_code == 200
    assert res_me.json()["must_change_password"] is True


def test_reject_admin123_as_new_password(client):
    # Login as admin
    login_res = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    ).json()
    token = login_res["access_token"]
    
    # Attempt to change to admin123 should be rejected with 400
    res = client.post(
        "/api/auth/change-password",
        json={"old_password": "admin123", "new_password": "admin123"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 400


def test_successful_password_change_clears_flag(client):
    # Login as admin
    login_res = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    ).json()
    token = login_res["access_token"]
    
    # Change password to new secure password
    res = client.post(
        "/api/auth/change-password",
        json={"old_password": "admin123", "new_password": "SuperSecretPass123!"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200
    assert res.json()["message"] == "Password changed successfully"
    
    # Now login with the new password
    new_login_res = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "SuperSecretPass123!"},
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert new_login_res.status_code == 200
    new_body = new_login_res.json()
    assert new_body["user"]["must_change_password"] is False
    
    # /api/auth/me also reports False
    new_token = new_body["access_token"]
    res_me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert res_me.status_code == 200
    assert res_me.json()["must_change_password"] is False


def test_jwt_secret_auto_generation(tmp_path, monkeypatch):
    import auth.config as auth_config_module
    import os
    
    test_env = tmp_path / ".env"
    monkeypatch.setattr(auth_config_module, "ENV_PATHS", [test_env])
    monkeypatch.setattr(auth_config_module, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    
    secret = auth_config_module._ensure_jwt_secret()
    assert secret, "Secret was not generated"
    assert len(secret) >= 32
    assert test_env.exists(), ".env file was not created"
    content = test_env.read_text(encoding="utf-8")
    assert f"JWT_SECRET_KEY={secret}" in content


# --------------------------------------------------------------------
# Database Backup Tests
# --------------------------------------------------------------------

def test_database_backup_api_flow(client, tmp_path, monkeypatch):
    from services.backup_service import backup_service
    
    # Isolate backup directory to tmp_path
    monkeypatch.setattr(backup_service, "backup_dir", tmp_path / "test_backups")
    
    # 1. Initially empty
    res_list = client.get("/api/database/backups")
    assert res_list.status_code == 200
    assert res_list.json() == []
    
    # 2. Trigger on-demand backup
    res_backup = client.post("/api/database/backup")
    assert res_backup.status_code == 200
    data = res_backup.json()
    assert data["success"] is True
    assert "backup" in data
    filename = data["backup"]["filename"]
    assert filename.startswith("ping_monitor_backup_")
    assert filename.endswith(".db")
    assert data["backup"]["size_bytes"] > 0
    
    # 3. List backups should now contain 1
    res_list_after = client.get("/api/database/backups")
    assert res_list_after.status_code == 200
    backups = res_list_after.json()
    assert len(backups) == 1
    assert backups[0]["filename"] == filename
    
    # 4. Download backup
    res_dl = client.get(f"/api/database/backups/{filename}/download")
    assert res_dl.status_code == 200
    assert len(res_dl.content) > 0
    assert res_dl.headers.get("content-type") == "application/x-sqlite3"
    
    # 5. Delete backup
    res_del = client.delete(f"/api/database/backups/{filename}")
    assert res_del.status_code == 204
    
    # 6. Verify deleted
    res_list_empty = client.get("/api/database/backups")
    assert res_list_empty.status_code == 200
    assert res_list_empty.json() == []


def test_backup_cleanup_retention(tmp_path):
    from services.backup_service import BackupService
    
    svc = BackupService(backup_dir=tmp_path / "retention_backups")
    
    # Create 5 dummy backup files
    for i in range(5):
        svc.create_backup(custom_name=f"test_{i}.db", keep_count=999)
        
    assert len(svc.list_backups()) == 5
    
    # Prune keeping only 2
    pruned = svc.cleanup_old_backups(keep_count=2)
    assert pruned == 3
    assert len(svc.list_backups()) == 2


def test_upload_and_restore_backup(client, tmp_path, monkeypatch):
    import io
    from services.backup_service import backup_service

    monkeypatch.setattr(backup_service, "backup_dir", tmp_path / "test_upload_backups")

    # 1. Create a target first
    t1 = client.post(
        "/api/targets",
        json={"name": "Target Alpha", "host": "127.0.0.1", "interval": 10, "count": 2, "timeout": 1, "enabled": True}
    ).json()

    # 2. Take a backup of this state
    res_backup = client.post("/api/database/backup")
    assert res_backup.status_code == 200
    backup_filename = res_backup.json()["backup"]["filename"]

    # 3. Download the backup content
    res_dl = client.get(f"/api/database/backups/{backup_filename}/download")
    backup_bytes = res_dl.content

    # 4. Now modify live DB by adding a second target
    t2 = client.post(
        "/api/targets",
        json={"name": "Target Beta", "host": "127.0.0.1", "interval": 10, "count": 2, "timeout": 1, "enabled": True}
    ).json()

    targets_now = client.get("/api/targets").json()
    assert len(targets_now) == 2

    # 5. Upload the backup file with custom name
    upload_file = io.BytesIO(backup_bytes)
    res_upload = client.post(
        "/api/database/upload",
        files={"file": ("my_uploaded_backup.db", upload_file, "application/x-sqlite3")}
    )
    assert res_upload.status_code == 200
    uploaded_info = res_upload.json()
    assert uploaded_info["success"] is True
    uploaded_filename = uploaded_info["backup"]["filename"]

    # 6. Test reject invalid upload
    bad_file = io.BytesIO(b"This is not a sqlite database")
    res_bad = client.post(
        "/api/database/upload",
        files={"file": ("fake.db", bad_file, "application/x-sqlite3")}
    )
    assert res_bad.status_code == 400

    # 7. Restore from the uploaded backup (which had only 1 target)
    res_restore = client.post(f"/api/database/backups/{uploaded_filename}/restore")
    assert res_restore.status_code == 200
    assert res_restore.json()["success"] is True

    # 8. Verify targets after restore (should only have Target Alpha, not Beta)
    targets_restored = client.get("/api/targets").json()
    assert len(targets_restored) == 1
    assert targets_restored[0]["name"] == "Target Alpha"


def test_default_root_group_exists(client):
    res = client.get("/api/groups")
    assert res.status_code == 200
    groups = res.json()
    assert len(groups) >= 1
    root = next((g for g in groups if g["name"].lower() == "root"), None)
    assert root is not None
    assert root["name"] == "Root"


def test_create_target_defaults_to_root_group(client):
    res = client.post(
        "/api/targets",
        json={"name": "NoGroupTarget", "host": "127.0.0.1", "interval": 10, "count": 2, "timeout": 1, "enabled": True}
    )
    assert res.status_code == 201
    target = res.json()
    assert target["group_name"] == "Root"
    assert target["group_id"] is not None

    # Verify via get
    res_get = client.get(f"/api/targets/{target['id']}")
    assert res_get.status_code == 200
    assert res_get.json()["group_name"] == "Root"


def test_groups_crud_and_target_integration(client):
    # 1. Create a new group
    res_g = client.post("/api/groups", json={"name": "Servers", "description": "Production Servers"})
    assert res_g.status_code == 201
    group = res_g.json()
    assert group["name"] == "Servers"
    assert group["description"] == "Production Servers"
    gid = group["id"]

    # 2. Duplicate group name rejected
    res_dup = client.post("/api/groups", json={"name": "servers"})
    assert res_dup.status_code == 409

    # 3. Create target with this group_id
    res_t1 = client.post(
        "/api/targets",
        json={"name": "Web01", "host": "127.0.0.1", "interval": 10, "count": 2, "timeout": 1, "enabled": True, "group_id": gid}
    )
    assert res_t1.status_code == 201
    t1 = res_t1.json()
    assert t1["group_name"] == "Servers"
    assert t1["group_id"] == gid

    # 4. Create target with group_name
    res_t2 = client.post(
        "/api/targets",
        json={"name": "Web02", "host": "127.0.0.1", "interval": 10, "count": 2, "timeout": 1, "enabled": True, "group_name": "Routers"}
    )
    assert res_t2.status_code == 201
    t2 = res_t2.json()
    assert t2["group_name"] == "Routers"

    # 5. Check target counts in list_groups
    groups_list = client.get("/api/groups").json()
    servers_g = next((g for g in groups_list if g["name"] == "Servers"), None)
    assert servers_g is not None
    assert servers_g["target_count"] == 1

    routers_g = next((g for g in groups_list if g["name"] == "Routers"), None)
    assert routers_g is not None
    assert routers_g["target_count"] == 1

    # 6. Update target to different group
    res_update_t = client.put(f"/api/targets/{t1['id']}", json={"group_id": routers_g["id"]})
    assert res_update_t.status_code == 200
    assert res_update_t.json()["group_name"] == "Routers"
    assert res_update_t.json()["group_id"] == routers_g["id"]

    # 7. Update group details
    res_update_g = client.put(f"/api/groups/{gid}", json={"name": "Servers-Renamed", "description": "Updated desc"})
    assert res_update_g.status_code == 200
    assert res_update_g.json()["name"] == "Servers-Renamed"

    # 8. Delete group reassigns targets to Root
    res_del_routers = client.delete(f"/api/groups/{routers_g['id']}")
    assert res_del_routers.status_code == 204

    # Target t1 and t2 should now belong to Root
    t1_after = client.get(f"/api/targets/{t1['id']}").json()
    assert t1_after["group_name"] == "Root"
    t2_after = client.get(f"/api/targets/{t2['id']}").json()
    assert t2_after["group_name"] == "Root"

    # 9. Cannot delete Root group
    root_g = next(g for g in client.get("/api/groups").json() if g["name"].lower() == "root")
    res_del_root = client.delete(f"/api/groups/{root_g['id']}")
    assert res_del_root.status_code == 400

    # 10. Cannot rename Root group
    res_ren_root = client.put(f"/api/groups/{root_g['id']}", json={"name": "OtherName"})
    assert res_ren_root.status_code == 400


def test_nested_groups_hierarchy_and_cycles(client):
    # 1. Create Parent Group
    res_p = client.post("/api/groups", json={"name": "Bank Jateng", "description": "Bank Jateng Group"})
    assert res_p.status_code == 201
    parent = res_p.json()
    assert parent["name"] == "Bank Jateng"
    p_id = parent["id"]

    # 2. Create Child Subgroup
    res_c = client.post("/api/groups", json={"name": "KC Temanggung", "description": "Kantor Cabang", "parent_id": p_id})
    assert res_c.status_code == 201
    child = res_c.json()
    assert child["name"] == "KC Temanggung"
    assert child["parent_id"] == p_id
    assert child["parent_name"] == "Bank Jateng"
    assert child["level"] == 1
    assert "Bank Jateng / KC Temanggung" in child["full_path"]
    c_id = child["id"]

    # 3. Create Grandchild Subgroup
    res_gc = client.post("/api/groups", json={"name": "ATM Temanggung", "parent_id": c_id})
    assert res_gc.status_code == 201
    grandchild = res_gc.json()
    assert grandchild["level"] == 2
    assert "Bank Jateng / KC Temanggung / ATM Temanggung" in grandchild["full_path"]

    # 4. Prevent self-parenting
    res_cycle = client.put(f"/api/groups/{p_id}", json={"parent_id": p_id})
    assert res_cycle.status_code == 400

    # 5. Prevent parenting Root group
    root_g = next(g for g in client.get("/api/groups").json() if g["name"].lower() == "root")
    res_root_p = client.put(f"/api/groups/{root_g['id']}", json={"parent_id": p_id})
    assert res_root_p.status_code == 200
    assert res_root_p.json()["parent_id"] is None


def test_settings_get_and_update(client):
    # 1. Default settings
    res = client.get("/api/settings")
    assert res.status_code == 200
    data = res.json()
    assert data["site_name"] == "PingOn"
    assert data["site_tagline"] == "Keep Your Network On."

    # 2. Update site name & tagline
    res = client.put("/api/settings", json={"site_name": "My Network Monitor", "site_tagline": "Monitoring 24/7"})
    assert res.status_code == 200
    data = res.json()
    assert data["site_name"] == "My Network Monitor"
    assert data["site_tagline"] == "Monitoring 24/7"

    # 3. GET reflects updated values
    res = client.get("/api/settings")
    assert res.status_code == 200
    assert res.json()["site_name"] == "My Network Monitor"


def test_settings_logo_and_favicon_upload(client):
    import io
    from PIL import Image

    # 1. Create a large test image (500x300 px) to verify resizing to max 250x100
    img = Image.new("RGBA", (500, 300), color=(255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    # Upload logo
    res = client.post(
        "/api/settings/logo",
        files={"file": ("large_logo.png", buf, "image/png")}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["logo_url"] is not None
    assert data["use_logo"] is True

    # Verify uploaded logo dimensions on disk
    from api.routes_settings import UPLOAD_DIR
    filename = data["logo_url"].replace("/uploads/", "")
    saved_img = Image.open(UPLOAD_DIR / filename)
    w, h = saved_img.size
    assert w <= 250
    assert h <= 100

    # 2. Create favicon test image (128x128 px) to verify resizing to exactly 32x32
    fav = Image.new("RGBA", (128, 128), color=(0, 255, 0, 255))
    fav_buf = io.BytesIO()
    fav.save(fav_buf, format="PNG")
    fav_buf.seek(0)

    # Upload favicon
    res_fav = client.post(
        "/api/settings/favicon",
        files={"file": ("test_favicon.png", fav_buf, "image/png")}
    )
    assert res_fav.status_code == 200
    fav_data = res_fav.json()
    assert fav_data["favicon_url"] is not None

    fav_filename = fav_data["favicon_url"].replace("/uploads/", "")
    saved_fav = Image.open(UPLOAD_DIR / fav_filename)
    assert saved_fav.size == (32, 32)

    # 3. Delete logo & delete favicon
    del_logo = client.delete("/api/settings/logo")
    assert del_logo.status_code == 200
    assert del_logo.json()["logo_url"] is None
    assert del_logo.json()["use_logo"] is False

    del_fav = client.delete("/api/settings/favicon")
    assert del_fav.status_code == 200
    assert del_fav.json()["favicon_url"] is None

    # 4. Reset settings
    res_reset = client.post("/api/settings/reset")
    assert res_reset.status_code == 200
    assert res_reset.json()["site_name"] == "PingOn"




