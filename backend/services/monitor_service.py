"""Background continuous-monitoring service.

Key design points (see prompt sections 6, 7, 32, 37):

- Exactly ONE asyncio task per enabled target, created once at startup
  (or when a target is added/enabled) and cancelled on delete/disable.
  Frontend polling never spawns pings — it only ever reads the
  in-memory state this service maintains (`get_dashboard_state`).
- The actual `ping.PingMonitor.run()` call is synchronous (it shells
  out via subprocess), so it's dispatched through `asyncio.to_thread`
  to avoid blocking the event loop — this is the "asyncio secara
  tepat" requirement, not a new Thread per cycle.
- `PingMonitor` (from the existing engine) already implements the
  raw_status vs effective_status flapping-protection logic — this
  service reuses it verbatim rather than re-implementing it.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

from ping import PingMonitor, PingResult

import db

logger = logging.getLogger("ping_monitor.service")

DOWN_THRESHOLD = 2
UP_THRESHOLD = 2

# PAUSED is a monitoring-lifecycle state layered on top of the ping
# engine — it is intentionally NOT part of ping.PingStatus, since the
# engine itself must stay untouched (it only ever reports UP/DOWN/
# DEGRADED/ERROR for an actual ping it performed). PAUSED means "no
# ping was performed at all", which is a service-level concept.
PAUSED = "PAUSED"


@dataclass
class TargetState:
    """In-memory snapshot of a target's latest monitoring cycle.

    This is what dashboard/target-list endpoints read from — it is
    never recomputed on-demand from the DB, so GET requests are cheap
    and never trigger a ping.
    """

    raw_status: str = "UNKNOWN"
    status: str = "UNKNOWN"  # effective_status
    latency_min: Optional[float] = None
    latency_avg: Optional[float] = None
    latency_max: Optional[float] = None
    packet_loss: Optional[float] = None
    last_check: Optional[str] = None
    status_changed: bool = False
    previous_status: Optional[str] = None
    error: Optional[str] = None


class MonitorService:
    def __init__(self) -> None:
        self._monitors: dict[int, PingMonitor] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._state: dict[int, TargetState] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------

    async def start_all(self) -> None:
        """Start background loops for every enabled target in the DB.
        Called once at FastAPI startup."""
        targets = db.list_targets()
        for t in targets:
            if t["enabled"]:
                await self._spawn(t)

    async def stop_all(self) -> None:
        for target_id in list(self._tasks.keys()):
            await self._cancel(target_id)

    # -- target lifecycle hooks (called by the targets API) ---------

    async def on_target_created(self, target: dict) -> None:
        if target["enabled"]:
            await self._spawn(target)

    async def on_target_updated(self, target: dict) -> None:
        # Simplest correct approach: always restart so the new
        # interval/count/timeout/host take effect immediately, and so
        # enabling/disabling is handled by the same code path.
        await self._cancel(target["id"])
        if target["enabled"]:
            await self._spawn(target)

    async def on_target_deleted(self, target_id: int) -> None:
        await self._cancel(target_id)
        self._state.pop(target_id, None)

    # -- pause / resume ------------------------------------------------
    #
    # Pause is implemented as: cancel the task FIRST, THEN record the
    # transition — never "ping, then mark paused". Cancelling the task
    # is synchronous-enough (awaited) that no further cycle can start
    # afterward, so a paused target is genuinely never pinged again
    # until resumed. Persisting `enabled=False` in the DB (done by the
    # caller, routes_targets.py, before calling this) is what makes the
    # skip survive an app restart too — start_all() only spawns tasks
    # for enabled targets.

    async def pause(self, target: dict) -> None:
        target_id = target["id"]
        await self._cancel(target_id)

        now = _now_iso()
        self._state[target_id] = TargetState(
            raw_status=PAUSED,
            status=PAUSED,
            last_check=now,
        )

        # Record the pause as a single history marker (not a repeating
        # fake ping) so the chart can visually distinguish a paused
        # span from a real DOWN span, without ever writing a DOWN row
        # for a target that was never actually checked.
        await asyncio.to_thread(
            db.insert_result,
            target_id,
            now,
            PAUSED,
            PAUSED,
            None,
            None,
            None,
            0.0,
            0,
            0,
        )
        logger.info("Paused monitoring target_id=%s", target_id)

    async def resume(self, target: dict) -> None:
        target_id = target["id"]
        # Deliberately reset to UNKNOWN rather than restoring the last
        # pre-pause status: resuming must never make the UI claim UP
        # (or DOWN) before an actual post-resume check has run. A fresh
        # PingMonitor is created in _spawn(), so flapping counters also
        # start clean — the very next real result decides the status.
        self._state[target_id] = TargetState()
        await self._spawn(target)
        logger.info("Resumed monitoring target_id=%s", target_id)

    # -- state access (read-only, no side effects) -------------------

    def get_state(self, target_id: int) -> TargetState:
        return self._state.get(target_id, TargetState())

    # -- internals ----------------------------------------------------

    async def _spawn(self, target: dict) -> None:
        target_id = target["id"]
        async with self._lock:
            if target_id in self._tasks and not self._tasks[target_id].done():
                return  # already running
            monitor = PingMonitor(
                host=target["host"],
                count=target["count"],
                timeout=target["timeout"],
                interval=0.2,
                down_threshold=DOWN_THRESHOLD,
                up_threshold=UP_THRESHOLD,
            )
            self._monitors[target_id] = monitor
            self._state.setdefault(target_id, TargetState())
            task = asyncio.create_task(self._loop(target_id, target["interval"]))
            self._tasks[target_id] = task
            logger.info("Started monitoring task target_id=%s host=%s", target_id, target["host"])

    async def _cancel(self, target_id: int) -> None:
        async with self._lock:
            task = self._tasks.pop(target_id, None)
            self._monitors.pop(target_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("Stopped monitoring task target_id=%s", target_id)

    async def _loop(self, target_id: int, interval_seconds: float) -> None:
        monitor = self._monitors[target_id]
        try:
            while True:
                await self._run_cycle(target_id, monitor)
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a monitoring loop must never die silently
            logger.exception("Monitoring loop crashed for target_id=%s", target_id)

    async def _run_cycle(self, target_id: int, monitor: PingMonitor) -> None:
        result: PingResult = await asyncio.to_thread(monitor.run)

        state = TargetState(
            raw_status=monitor.raw_status.value if monitor.raw_status else "UNKNOWN",
            status=result.status.value,
            latency_min=result.latency.min,
            latency_avg=result.latency.avg,
            latency_max=result.latency.max,
            packet_loss=result.packet_loss,
            last_check=result.timestamp,
            status_changed=bool(result.status_changed),
            previous_status=result.previous_status.value if result.previous_status else None,
            error=result.error,
        )
        self._state[target_id] = state

        if result.status_changed:
            logger.info(
                "Status change target_id=%s %s -> %s",
                target_id,
                state.previous_status,
                state.status,
            )

        await asyncio.to_thread(
            db.insert_result,
            target_id,
            result.timestamp,
            result.status.value,
            state.raw_status,
            result.latency.min,
            result.latency.avg,
            result.latency.max,
            result.packet_loss,
            result.sent,
            result.received,
        )

        # --- Telegram notification on status change ---
        if result.status_changed and state.previous_status not in (None, PAUSED, "UNKNOWN"):
            await self._maybe_send_telegram(target_id, state, result)

    async def _maybe_send_telegram(self, target_id: int, state: TargetState, result) -> None:
        """Send Telegram notification if target has it configured."""
        try:
            target = await asyncio.to_thread(db.get_target, target_id)
            if not target or not target.get("telegram_enabled"):
                return

            current = state.status
            # Determine the event type
            if current in ("DOWN", "ERROR", "UNKNOWN", "DEGRADED"):
                event = "DOWN"
                if not target.get("telegram_notify_down"):
                    return
            elif current == "UP":
                event = "UP"
                if not target.get("telegram_notify_up"):
                    return
            else:
                return

            token = target.get("telegram_token")
            chat_id = target.get("telegram_chat_id")
            if not token or not chat_id:
                return

            # Determine custom template
            custom_template = None
            if target.get("telegram_custom"):
                if event == "DOWN":
                    custom_template = target.get("telegram_template_down")
                else:
                    custom_template = target.get("telegram_template_up")

            from services.telegram_service import telegram_service
            text = telegram_service.format_message(target, event, result, custom_template)
            await telegram_service.send_message(token, chat_id, text)
            logger.info("Telegram notification sent target_id=%s event=%s", target_id, event)
        except Exception:
            logger.exception("Failed to send Telegram notification target_id=%s", target_id)


# Module-level singleton, imported by the API routers and main.py.
monitor_service = MonitorService()


def _now_iso() -> str:
    """Same timezone-aware ISO format used by db._now_iso / the ping
    engine's own timestamps, kept local to avoid reaching into db's
    "private" helper."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
