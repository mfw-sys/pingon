#!/usr/bin/env python3
"""Standalone SQLite Database Backup CLI Script.

Can be run standalone or added to cron / Task Scheduler:
    python backend/backup.py
    python backend/backup.py --list
    python backend/backup.py --clean --keep 10
    python backend/backup.py --name my_custom_backup.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db
from services.backup_service import backup_service, format_size


def main():
    parser = argparse.ArgumentParser(description="SQLite Database Backup Utility for PingOn")
    parser.add_argument("--list", "-l", action="store_true", help="List all existing backups")
    parser.add_argument("--name", "-n", type=str, default=None, help="Custom filename for the backup")
    parser.add_argument("--keep", "-k", type=int, default=14, help="Number of backups to retain (default: 14)")
    parser.add_argument("--clean", "-c", action="store_true", help="Clean up old backups exceeding retention limit")

    args = parser.parse_args()

    # Ensure DB directory / init check
    db.init_db()

    if args.list:
        backups = backup_service.list_backups()
        if not backups:
            print("No backups found in", backup_service.backup_dir)
            return
        print(f"Backups in {backup_service.backup_dir} ({len(backups)} total):")
        print(f"{'Filename':<45} {'Size':<12} {'Created At'}")
        print("-" * 80)
        for b in backups:
            print(f"{b['filename']:<45} {b['size_formatted']:<12} {b['created_at']}")
        return

    if args.clean:
        pruned = backup_service.cleanup_old_backups(keep_count=args.keep)
        print(f"Cleaned up {pruned} old backup(s) (retaining newest {args.keep}).")
        return

    print("Creating SQLite database backup...")
    try:
        result = backup_service.create_backup(custom_name=args.name, keep_count=args.keep)
        print(f"[SUCCESS] Backup saved to: {result['path']}")
        print(f"          Size: {result['size_formatted']} ({result['size_bytes']} bytes)")
        print(f"          Timestamp: {result['created_at']}")
    except Exception as exc:
        print(f"[ERROR] Backup failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
