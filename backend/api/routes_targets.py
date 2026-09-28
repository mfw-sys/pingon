"""Target management + history endpoints.

`POST/PUT/DELETE` here are the only places that mutate targets, and
each one calls into `monitor_service` so the background monitoring
task set stays in sync with the DB â€” the frontend never manages
monitoring tasks directly.

All endpoints are `async def` and DB access is dispatched via
`asyncio.to_thread`: sqlite3 calls are blocking, and awaiting
`monitor_service.on_target_*` requires a running event loop in the
current thread, which a sync (threadpool-executed) endpoint would not
reliably have.
"""

from __future__ import annotations

import asyncio

from typing import Annotated
from fastapi import APIRouter, HTTPException, Depends
from auth.dependencies import get_current_user, require_role

import db
from models.schemas import TargetCreate, TargetOut, TargetUpdate, TargetHistoryResponse, TelegramTestRequest
from ping.exceptions import InvalidHostError
from ping.ping_engine import validate_host
from services.monitor_service import monitor_service
from services.telegram_service import telegram_service

router = APIRouter(prefix="/api/targets", tags=["targets"])


def _validate_host_or_422(host: str) -> None:
    try:
        validate_host(host)
    except InvalidHostError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid host or hostname: {exc}",
        ) from exc


@router.get("", response_model=list[TargetOut])
async def list_targets(current_user: Annotated[dict, Depends(get_current_user)]) -> list[dict]:
    targets = await asyncio.to_thread(db.list_targets)
    return [_mask_token(t) for t in targets]


@router.post("/test-telegram")
async def test_telegram(payload: TelegramTestRequest, current_user: Annotated[dict, Depends(require_role('administrator'))]) -> dict:
    """Send a test message to verify Telegram bot token and chat ID."""
    test_message = (
        f"âœ… <b>PingOn Test Notification</b>\n\n"
        f"This is a test message from PingOn.\n"
        f"Configuration: {payload.name}\n"
        f"Your Telegram notification is working correctly!"
    )
    try:
        await telegram_service.send_message(payload.token, payload.chat_id, test_message)
        return {"success": True, "message": "Telegram notification sent successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("", response_model=TargetOut, status_code=201)
async def create_target(payload: TargetCreate, current_user: Annotated[dict, Depends(require_role('administrator'))]) -> dict:
    _validate_host_or_422(payload.host)
    target = await asyncio.to_thread(
        db.create_target,
        payload.name,
        payload.host,
        payload.interval,
        payload.count,
        payload.timeout,
        payload.enabled,
        payload.telegram_enabled,
        payload.telegram_name,
        payload.telegram_token,
        payload.telegram_chat_id,
        payload.telegram_notify_down,
        payload.telegram_notify_up,
        payload.telegram_custom,
        payload.telegram_template_down,
        payload.telegram_template_up,
        payload.group_id,
        payload.group_name
    )
    await monitor_service.on_target_created(target)
    return _mask_token(target)


@router.get("/{target_id}", response_model=TargetOut)
async def get_target(target_id: int, current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    target = await asyncio.to_thread(db.get_target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")
    return _mask_token(target)


@router.put("/{target_id}", response_model=TargetOut)
async def update_target(target_id: int, payload: TargetUpdate, current_user: Annotated[dict, Depends(require_role('administrator'))]) -> dict:
    existing = await asyncio.to_thread(db.get_target, target_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Target not found")

    if payload.host is not None:
        _validate_host_or_422(payload.host)

    fields = payload.model_dump(exclude_unset=True)
    # If the frontend sends back a masked token, don't overwrite the real one
    if "telegram_token" in fields and fields["telegram_token"] and "****" in fields["telegram_token"]:
        del fields["telegram_token"]
    updated = await asyncio.to_thread(db.update_target, target_id, **fields)

    # A PUT that flips `enabled` is a pause/resume transition and must
    # go through the same proper pause()/resume() path as the dedicated
    # endpoints below (history marker, state reset, etc.) â€” not just a
    # bare task restart â€” so PAUSED stays correct no matter which route
    # the client used to toggle it.
    if "enabled" in fields and bool(fields["enabled"]) != bool(existing["enabled"]):
        if updated["enabled"]:
            await monitor_service.resume(updated)
        else:
            await monitor_service.pause(updated)
    else:
        await monitor_service.on_target_updated(updated)

    return _mask_token(updated)


@router.post("/{target_id}/pause")
async def pause_target(target_id: int, current_user: Annotated[dict, Depends(require_role('administrator'))]) -> dict:
    target = await asyncio.to_thread(db.get_target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    if not target["enabled"]:
        # Already paused â€” idempotent, not an error.
        return {"id": target_id, "status": "PAUSED"}

    updated = await asyncio.to_thread(db.update_target, target_id, enabled=False)
    await monitor_service.pause(updated)
    return {"id": target_id, "status": "PAUSED"}


@router.post("/{target_id}/resume")
async def resume_target(target_id: int, current_user: Annotated[dict, Depends(require_role('administrator'))]) -> dict:
    target = await asyncio.to_thread(db.get_target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    if target["enabled"]:
        # Already active â€” idempotent, not an error.
        return {"id": target_id, "status": "UNKNOWN"}

    updated = await asyncio.to_thread(db.update_target, target_id, enabled=True)
    await monitor_service.resume(updated)
    # Status is intentionally "UNKNOWN", not "UP" â€” the real status is
    # only known once the next actual check completes (see
    # monitor_service.resume()); the frontend should treat this as
    # "checkingâ€¦", not as a confirmed state.
    return {"id": target_id, "status": "UNKNOWN"}


@router.delete("/{target_id}", status_code=204)
async def delete_target(target_id: int, current_user: Annotated[dict, Depends(require_role('administrator'))]) -> None:
    existing = await asyncio.to_thread(db.get_target, target_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Target not found")

    deleted = await asyncio.to_thread(db.delete_target, target_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Target not found")

    await monitor_service.on_target_deleted(target_id)


@router.get("/{target_id}/history", response_model=TargetHistoryResponse)
async def get_target_history(target_id: int, current_user: Annotated[dict, Depends(get_current_user)], hours: int = 24) -> dict:
    target = await asyncio.to_thread(db.get_target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")
    if hours < 1 or hours > 24 * 30:
        raise HTTPException(status_code=422, detail="hours must be between 1 and 720")
    
    rows = await asyncio.to_thread(db.get_history, target_id, hours)
    
    import datetime
    
    uptime = 0
    downtime = 0
    incidents = 0
    
    if not rows:
        return {"summary": {"availability": None, "uptime": 0, "downtime": 0, "incidents": 0}, "heartbeat": []}
    
    def classify(s: str) -> str:
        s = s.upper()
        if s == "PAUSED": return "PAUSED"
        if s == "UP": return "UP"
        if s == "DEGRADED": return "DEGRADED"
        return "DOWN"
        
    def is_down(kind: str) -> bool:
        return kind in ("DOWN", "DEGRADED")
    
    segments = []
    for r in rows:
        ts = datetime.datetime.fromisoformat(r["timestamp"]).timestamp() * 1000
        kind = classify(r["status"])
        if segments and segments[-1]["kind"] == kind:
            pass # Same segment
        else:
            segments.append({"kind": kind, "start": ts})
            
    now = datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
    for i in range(len(segments)):
        if i < len(segments) - 1:
            segments[i]["end"] = segments[i+1]["start"]
        else:
            segments[i]["end"] = now

    for seg in segments:
        duration = seg["end"] - seg["start"]
        if seg["kind"] == "UP":
            uptime += duration
        elif is_down(seg["kind"]):
            downtime += duration
            incidents += 1
            
    total_tracked = uptime + downtime
    availability = (uptime / total_tracked * 100) if total_tracked > 0 else None
    
    return {
        "summary": {
            "availability": availability,
            "uptime": int(uptime),
            "downtime": int(downtime),
            "incidents": incidents
        },
        "heartbeat": rows
    }


def _mask_token(target: dict) -> dict:
    """Mask the telegram_token before returning to the frontend."""
    if target and target.get("telegram_token"):
        token = target["telegram_token"]
        if len(token) > 8:
            target = {**target, "telegram_token": token[:4] + ":" + "*" * (len(token) - 8) + token[-4:]}
        else:
            target = {**target, "telegram_token": "****"}
    return target
