from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from PIL import Image

import db
from auth.dependencies import require_role
from models.schemas import SettingsOut, SettingsUpdate

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
)


def _cleanup_old_file(url_path: str | None) -> None:
    if not url_path or not url_path.startswith("/uploads/"):
        return
    filename = url_path.replace("/uploads/", "").strip()
    if filename:
        file_path = UPLOAD_DIR / filename
        try:
            if file_path.exists() and file_path.is_file():
                file_path.unlink()
        except OSError:
            pass


@router.get("", response_model=SettingsOut)
async def get_settings() -> dict[str, Any]:
    """Retrieve system settings (publicly accessible for login and app header)."""
    return db.get_all_settings()


@router.put("", response_model=SettingsOut)
async def update_settings(
    payload: SettingsUpdate,
    _: dict = Depends(require_role("administrator")),
) -> dict[str, Any]:
    """Update general website identity settings (administrator only)."""
    data = payload.model_dump(exclude_unset=True)
    if not data:
        return db.get_all_settings()
    return db.update_settings(data)


@router.post("/logo", response_model=SettingsOut)
async def upload_logo(
    file: UploadFile = File(...),
    _: dict = Depends(require_role("administrator")),
) -> dict[str, Any]:
    """Upload and process website logo.
    
    If dimensions exceed 250 x 100, it is automatically resized/fitted to max 250 x 100.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename missing")

    raw_data = await file.read()
    if len(raw_data) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(raw_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds 10MB limit")

    ext = Path(file.filename).suffix.lower()
    timestamp = int(time.time() * 1000)

    if ext == ".svg":
        filename = f"logo_{timestamp}.svg"
        dest_path = UPLOAD_DIR / filename
        dest_path.write_bytes(raw_data)
    else:
        try:
            image = Image.open(io.BytesIO(raw_data))
            # Format preservation or PNG for alpha channel support
            has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
            target_mode = "RGBA" if has_alpha else "RGB"
            if image.mode != target_mode:
                image = image.convert(target_mode)

            w, h = image.size
            max_w, max_h = 250, 100

            # If dimensions exceed 250 x 100, resize keeping aspect ratio
            if w > max_w or h > max_h:
                image.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)

            filename = f"logo_{timestamp}.png"
            dest_path = UPLOAD_DIR / filename
            image.save(dest_path, format="PNG", optimize=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid image file: {exc}") from exc

    current = db.get_all_settings()
    _cleanup_old_file(current.get("logo_url"))

    logo_url = f"/uploads/{filename}"
    return db.update_settings({"logo_url": logo_url, "use_logo": True})


@router.delete("/logo", response_model=SettingsOut)
async def remove_logo(
    _: dict = Depends(require_role("administrator")),
) -> dict[str, Any]:
    """Remove uploaded logo and revert to text title display."""
    current = db.get_all_settings()
    _cleanup_old_file(current.get("logo_url"))
    return db.update_settings({"logo_url": "", "use_logo": False})


@router.post("/favicon", response_model=SettingsOut)
async def upload_favicon(
    file: UploadFile = File(...),
    _: dict = Depends(require_role("administrator")),
) -> dict[str, Any]:
    """Upload and process favicon (automatically resized to 32x32 px)."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename missing")

    raw_data = await file.read()
    if len(raw_data) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(raw_data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds 5MB limit")

    timestamp = int(time.time() * 1000)

    try:
        image = Image.open(io.BytesIO(raw_data))
        has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
        target_mode = "RGBA" if has_alpha else "RGB"
        if image.mode != target_mode:
            image = image.convert(target_mode)

        # Exact 32x32 resize for favicon
        fav_image = image.resize((32, 32), Image.Resampling.LANCZOS)

        filename = f"favicon_{timestamp}.png"
        dest_path = UPLOAD_DIR / filename
        fav_image.save(dest_path, format="PNG", optimize=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image file: {exc}") from exc

    current = db.get_all_settings()
    _cleanup_old_file(current.get("favicon_url"))

    favicon_url = f"/uploads/{filename}"
    return db.update_settings({"favicon_url": favicon_url})


@router.delete("/favicon", response_model=SettingsOut)
async def remove_favicon(
    _: dict = Depends(require_role("administrator")),
) -> dict[str, Any]:
    """Remove custom favicon and revert to default."""
    current = db.get_all_settings()
    _cleanup_old_file(current.get("favicon_url"))
    return db.update_settings({"favicon_url": ""})


@router.post("/reset", response_model=SettingsOut)
async def reset_settings(
    _: dict = Depends(require_role("administrator")),
) -> dict[str, Any]:
    """Reset all website identity settings back to PingOn default."""
    current = db.get_all_settings()
    _cleanup_old_file(current.get("logo_url"))
    _cleanup_old_file(current.get("favicon_url"))
    return db.reset_settings()
