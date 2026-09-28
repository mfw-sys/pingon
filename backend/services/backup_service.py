"""SQLite Database Backup Service.

Provides:
1. Online, atomic SQLite backup using sqlite3's backup API (safe with active WAL mode).
2. Periodic automatic background backups with configurable interval and auto-rotation.
3. Backup listing, download, and deletion utilities.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import db

logger = logging.getLogger("ping_monitor.backup")

BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "backups"
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_KEEP_COUNT = int(os.getenv("BACKUP_KEEP_COUNT", "14"))
BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "true").lower() in ("1", "true", "yes")


def format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable string (KB, MB, GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


class BackupService:
    def __init__(self, backup_dir: Path = BACKUP_DIR):
        self.backup_dir = backup_dir
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def _get_backup_path(self, custom_name: Optional[str] = None) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if custom_name:
            clean_name = Path(custom_name).name
            if not clean_name.endswith(".db"):
                clean_name += ".db"
            return self.backup_dir / clean_name

        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.backup_dir / f"ping_monitor_backup_{now}.db"

    def create_backup(self, custom_name: Optional[str] = None, keep_count: int = BACKUP_KEEP_COUNT) -> dict[str, Any]:
        """Perform an online atomic backup of the SQLite database."""
        dest_path = self._get_backup_path(custom_name)

        logger.info("Starting SQLite database backup to %s", dest_path)
        with db.get_conn() as src_conn:
            dest_conn = sqlite3.connect(dest_path)
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()

        size_bytes = dest_path.stat().st_size
        created_at = datetime.datetime.fromtimestamp(dest_path.stat().st_mtime).isoformat()

        # Run auto-cleanup
        self.cleanup_old_backups(keep_count=keep_count)

        logger.info("Database backup created successfully: %s (%s)", dest_path.name, format_size(size_bytes))
        return {
            "filename": dest_path.name,
            "path": str(dest_path),
            "size_bytes": size_bytes,
            "size_formatted": format_size(size_bytes),
            "created_at": created_at,
        }

    def list_backups(self) -> list[dict[str, Any]]:
        """List all available SQLite backups sorted newest first."""
        if not self.backup_dir.exists():
            return []

        backups = []
        for file in self.backup_dir.glob("*.db"):
            stat = file.stat()
            backups.append({
                "filename": file.name,
                "path": str(file),
                "size_bytes": stat.st_size,
                "size_formatted": format_size(stat.st_size),
                "created_at": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

        backups.sort(key=lambda b: b["created_at"], reverse=True)
        return backups

    def get_backup_path(self, filename: str) -> Optional[Path]:
        """Get verified path for a specific backup filename (safe against directory traversal)."""
        clean_name = Path(filename).name
        file_path = self.backup_dir / clean_name
        if file_path.exists() and file_path.is_file() and clean_name.endswith(".db"):
            return file_path
        return None

    def delete_backup(self, filename: str) -> bool:
        """Delete a backup file safely."""
        file_path = self.get_backup_path(filename)
        if file_path and file_path.exists():
            file_path.unlink()
            logger.info("Deleted backup file: %s", filename)
            return True
        return False

    @staticmethod
    def validate_sqlite_file(file_path: Path) -> bool:
        """Verify that the file is a valid SQLite 3 database and has a sound schema."""
        if not file_path.exists() or file_path.stat().st_size < 100:
            return False

        with open(file_path, "rb") as f:
            header = f.read(16)
            if header != b"SQLite format 3\x00":
                return False

        try:
            conn = sqlite3.connect(file_path, timeout=5)
            try:
                # Check database integrity
                res = conn.execute("PRAGMA integrity_check").fetchone()
                if not res or res[0] != "ok":
                    return False

                # Check for table existence
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                if "targets" not in tables and "users" not in tables:
                    return False
                return True
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("SQLite validation failed for %s: %s", file_path, exc)
            return False

    def save_uploaded_backup(self, original_filename: str, content: bytes) -> dict[str, Any]:
        """Save and validate an uploaded backup file."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        clean_name = Path(original_filename).name.replace(" ", "_")
        if not clean_name.endswith(".db"):
            clean_name += ".db"

        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        target_name = f"ping_monitor_upload_{now}_{clean_name}"
        dest_path = self.backup_dir / target_name

        dest_path.write_bytes(content)

        if not self.validate_sqlite_file(dest_path):
            if dest_path.exists():
                dest_path.unlink()
            raise ValueError("The uploaded file is not a valid PingOn SQLite database.")

        size_bytes = dest_path.stat().st_size
        created_at = datetime.datetime.fromtimestamp(dest_path.stat().st_mtime).isoformat()

        logger.info("Uploaded backup saved successfully: %s (%s)", target_name, format_size(size_bytes))
        return {
            "filename": target_name,
            "path": str(dest_path),
            "size_bytes": size_bytes,
            "size_formatted": format_size(size_bytes),
            "created_at": created_at,
        }

    def restore_backup(self, filename: str) -> dict[str, Any]:
        """Restore the live database from a specified backup file.
        
        Creates a pre-restore safety snapshot first, then atomically restores the database.
        """
        backup_path = self.get_backup_path(filename)
        if not backup_path or not backup_path.exists():
            raise FileNotFoundError(f"Backup file '{filename}' not found.")

        if not self.validate_sqlite_file(backup_path):
            raise ValueError(f"Backup file '{filename}' is corrupted or invalid.")

        # 1. Take safety pre-restore backup
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safety_backup_name = f"ping_monitor_prerestore_{now}.db"
        pre_restore_info = self.create_backup(custom_name=safety_backup_name, keep_count=999)

        # 2. Perform atomic restore from backup into live database
        logger.warning("Restoring database from %s ...", backup_path.name)
        with sqlite3.connect(backup_path) as src_conn:
            with db.get_conn() as dest_conn:
                src_conn.backup(dest_conn)

        # 3. Ensure any schema migrations are applied
        db.init_db()

        logger.info("Database successfully restored from %s", backup_path.name)
        return {
            "restored_from": backup_path.name,
            "pre_restore_backup": pre_restore_info,
            "restored_at": datetime.datetime.now().isoformat(),
        }

    def cleanup_old_backups(self, keep_count: int = BACKUP_KEEP_COUNT) -> int:
        """Keep the latest N backups and delete older files."""
        if keep_count <= 0:
            return 0

        backups = self.list_backups()
        deleted_count = 0
        if len(backups) > keep_count:
            for old in backups[keep_count:]:
                old_path = Path(old["path"])
                if old_path.exists():
                    try:
                        old_path.unlink()
                        deleted_count += 1
                        logger.info("Pruned old backup: %s", old["filename"])
                    except Exception as exc:
                        logger.warning("Failed to delete old backup %s: %s", old["filename"], exc)
        return deleted_count

    async def _periodic_loop(self):
        """Background coroutine for periodic automated database backups."""
        interval_seconds = max(300, BACKUP_INTERVAL_HOURS * 3600)
        logger.info(
            "Periodic backup scheduler started (interval: %s hours, retention: %s backups)",
            BACKUP_INTERVAL_HOURS,
            BACKUP_KEEP_COUNT,
        )

        while self._running:
            try:
                # Sleep interval first, or initial delay
                await asyncio.sleep(interval_seconds)
                if not self._running:
                    break
                await asyncio.to_thread(self.create_backup)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error during periodic backup execution: %s", exc, exc_info=True)
                # Wait 5 minutes before retrying on error
                await asyncio.sleep(300)

    async def start(self):
        """Start the periodic backup background task."""
        if not BACKUP_ENABLED:
            logger.info("Automatic database backup is disabled via BACKUP_ENABLED=false")
            return

        if self._task is not None and not self._task.done():
            return

        self._running = True
        self._task = asyncio.create_task(self._periodic_loop())

    async def stop(self):
        """Stop the periodic backup background task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


backup_service = BackupService()
