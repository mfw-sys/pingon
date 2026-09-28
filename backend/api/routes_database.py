from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from auth.dependencies import require_role
from services.backup_service import backup_service
from services.monitor_service import monitor_service

router = APIRouter(
    prefix="/api/database",
    tags=["database"],
    dependencies=[Depends(require_role("administrator"))],
)


@router.post("/backup")
async def create_backup() -> dict[str, Any]:
    """Trigger an immediate online SQLite database backup."""
    try:
        result = await asyncio.to_thread(backup_service.create_backup)
        return {
            "success": True,
            "message": "Database backup created successfully",
            "backup": result,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backup creation failed: {exc}",
        ) from exc


@router.post("/upload")
async def upload_backup(file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload an existing SQLite database backup file."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename missing")

    try:
        content = await file.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        backup_info = await asyncio.to_thread(backup_service.save_uploaded_backup, file.filename, content)
        return {
            "success": True,
            "message": "Backup uploaded and validated successfully",
            "backup": backup_info,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to process uploaded backup: {exc}") from exc


@router.post("/backups/{filename}/restore")
async def restore_backup(filename: str) -> dict[str, Any]:
    """Restore database from a specific backup file and restart monitoring tasks."""
    try:
        result = await asyncio.to_thread(backup_service.restore_backup, filename)
        # Restart monitor loops to pick up restored targets
        await monitor_service.stop_all()
        await monitor_service.start_all()
        return {
            "success": True,
            "message": f"Database successfully restored from {filename}",
            "result": result,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc


@router.get("/backups")
async def list_backups() -> list[dict[str, Any]]:
    """List all available database backups."""
    return await asyncio.to_thread(backup_service.list_backups)


@router.get("/backups/{filename}/download")
async def download_backup(filename: str) -> FileResponse:
    """Download a specific SQLite database backup file."""
    file_path = backup_service.get_backup_path(filename)
    if not file_path or not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup file not found",
        )

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/x-sqlite3",
        headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'},
    )


@router.delete("/backups/{filename}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backup(filename: str):
    """Delete a specific database backup."""
    deleted = await asyncio.to_thread(backup_service.delete_backup, filename)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup file not found",
        )
    return None

