from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends

import db
from auth.dependencies import get_current_user
from models.schemas import (
    DashboardResponse,
    HealthResponse,
    PingRequest,
    PingResponse,
    TargetStateOut,
)
from ping import ping_host
from services.monitor_service import monitor_service

router = APIRouter(prefix="/api", tags=["monitor"])


@router.get("/health", response_model=HealthResponse)
async def health() -> dict:
    return {"status": "ok"}


@router.post("/ping", response_model=PingResponse)
async def ping_now(payload: PingRequest, current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    """Stateless, on-demand ping (the "Ping Now" button).

    Deliberately does NOT touch the background monitoring state or
    history table â€” it pings whatever host/params the frontend sends,
    independent of any saved target, and the response's latency comes
    straight from the ICMP engine's own RTT parsing (never HTTP round
    trip time).
    """
    result = await asyncio.to_thread(
        ping_host, payload.host, payload.count, payload.timeout, payload.interval
    )
    return result.to_dict()


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    targets = await asyncio.to_thread(db.list_targets)

    counts = {"UP": 0, "DEGRADED": 0, "DOWN": 0, "UNKNOWN": 0, "PAUSED": 0}
    target_states: list[dict] = []
    last_update: str | None = None

    for t in targets:
        # `enabled` in the DB is the single source of truth for PAUSED â€”
        # authoritative even if the in-memory state is stale (e.g. right
        # after an app restart, before any cycle has re-populated it).
        # A paused target is never handed to the scheduler, so its
        # in-memory state would otherwise just be whatever it was
        # before pausing (or UNKNOWN), which must not leak through here.
        if not t["enabled"]:
            counts["PAUSED"] += 1
            target_states.append(
                {
                    **t,
                    "raw_status": "PAUSED",
                    "status": "PAUSED",
                    "latency_min": None,
                    "latency_avg": None,
                    "latency_max": None,
                    "packet_loss": None,
                    "last_check": monitor_service.get_state(t["id"]).last_check,
                    "status_changed": False,
                }
            )
            continue

        state = monitor_service.get_state(t["id"])
        counts[state.status] = counts.get(state.status, 0) + 1
        if state.last_check and (last_update is None or state.last_check > last_update):
            last_update = state.last_check

        target_states.append(
            {
                **t,
                "raw_status": state.raw_status,
                "status": state.status,
                "latency_min": state.latency_min,
                "latency_avg": state.latency_avg,
                "latency_max": state.latency_max,
                "packet_loss": state.packet_loss,
                "last_check": state.last_check,
                "status_changed": state.status_changed,
            }
        )

    return {
        "total": len(targets),
        "up": counts["UP"],
        "degraded": counts["DEGRADED"],
        "down": counts["DOWN"],
        "unknown": counts["UNKNOWN"],
        "paused": counts["PAUSED"],
        "last_update": last_update,
        "targets": target_states,
    }
