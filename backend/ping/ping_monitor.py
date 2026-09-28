"""High level monitoring API: single-shot pings and continuous
monitoring with flapping protection.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .exceptions import (
    DnsResolutionError,
    InvalidHostError,
    PingExecutableNotFoundError,
    PingMonitorError,
    PingPermissionError,
)
from .logger import get_logger
from .models import LatencyStats, PingReply, PingResult, PingStatus
from .ping_engine import execute_native_ping, resolve_host, validate_host
from .ping_parser import ParsedReply

logger = get_logger()


def _compute_latency_stats(replies: list[PingReply]) -> LatencyStats:
    successful = [r.latency_ms for r in replies if r.success and r.latency_ms is not None]
    if not successful:
        return LatencyStats(min=None, max=None, avg=None)
    return LatencyStats(
        min=round(min(successful), 3),
        max=round(max(successful), 3),
        avg=round(sum(successful) / len(successful), 3),
    )


def _compute_packet_loss(sent: int, received: int) -> float:
    if sent <= 0:
        return 0.0
    loss = ((sent - received) / sent) * 100
    return max(0.0, min(100.0, round(loss, 3)))


def _determine_status(packet_loss: float) -> PingStatus:
    if packet_loss <= 0.0:
        return PingStatus.UP
    if packet_loss >= 100.0:
        return PingStatus.DOWN
    return PingStatus.DEGRADED


def _build_replies(parsed: list[ParsedReply], count: int) -> list[PingReply]:
    """Normalize parsed replies to exactly `count` entries.

    Native ping output can, in rare cases, produce fewer parsed lines
    than requested packets (e.g. the process was killed mid-batch). Any
    shortfall is padded as failed/lost packets rather than silently
    under-reporting `sent`, since we always send exactly `count`
    requests by construction.
    """
    replies: list[PingReply] = []
    for i in range(count):
        if i < len(parsed):
            p = parsed[i]
            replies.append(
                PingReply(sequence=i + 1, success=p.success, latency_ms=p.latency_ms)
            )
        else:
            replies.append(PingReply(sequence=i + 1, success=False, latency_ms=None))
    return replies


def ping_host(
    host: str,
    count: int = 4,
    timeout: float = 1.0,
    interval: float = 0.2,
) -> PingResult:
    """Ping `host` `count` times and return an accurate PingResult.

    Raises no exceptions for network-layer failures (timeouts, DNS
    failure, invalid host, missing ping binary, permission errors) —
    all of these are captured and returned as a PingResult with
    status=ERROR (for host/DNS/binary/permission problems) or a normal
    DOWN/DEGRADED/UP result (for timeouts, which are expected
    monitoring outcomes, not application errors).
    """
    logger.info("Ping started host=%s count=%s", host, count)

    try:
        validate_host(host)
    except InvalidHostError as exc:
        logger.warning("Invalid host: %s", exc)
        return PingResult(
            host=host,
            resolved_ip=None,
            status=PingStatus.ERROR,
            sent=0,
            received=0,
            packet_loss=0.0,
            latency=LatencyStats(),
            replies=[],
            error=str(exc),
        )

    try:
        resolved_ip = resolve_host(host)
    except DnsResolutionError as exc:
        logger.warning("DNS resolution failed: %s", exc)
        return PingResult(
            host=host,
            resolved_ip=None,
            status=PingStatus.ERROR,
            sent=0,
            received=0,
            packet_loss=0.0,
            latency=LatencyStats(),
            replies=[],
            error=str(exc),
        )

    try:
        execution = execute_native_ping(host, count, timeout, interval)
    except (PingExecutableNotFoundError, PingPermissionError) as exc:
        logger.error("Ping execution failed: %s", exc)
        return PingResult(
            host=host,
            resolved_ip=resolved_ip,
            status=PingStatus.ERROR,
            sent=0,
            received=0,
            packet_loss=0.0,
            latency=LatencyStats(),
            replies=[],
            error=str(exc),
        )
    except PingMonitorError as exc:
        logger.error("Unexpected ping-monitor error: %s", exc)
        return PingResult(
            host=host,
            resolved_ip=resolved_ip,
            status=PingStatus.ERROR,
            sent=0,
            received=0,
            packet_loss=0.0,
            latency=LatencyStats(),
            replies=[],
            error=str(exc),
        )

    replies = _build_replies(execution.replies, count)
    received = sum(1 for r in replies if r.success)
    packet_loss = _compute_packet_loss(count, received)
    status = _determine_status(packet_loss)
    latency = _compute_latency_stats(replies)

    for r in replies:
        if r.success:
            logger.info("Reply seq=%s latency=%.2fms", r.sequence, r.latency_ms)
        else:
            logger.warning("Timeout seq=%s", r.sequence)

    logger.info(
        "Result host=%s status=%s loss=%.1f%%", host, status.value, packet_loss
    )

    return PingResult(
        host=host,
        resolved_ip=resolved_ip,
        status=status,
        sent=count,
        received=received,
        packet_loss=packet_loss,
        latency=latency,
        replies=replies,
    )


class PingMonitor:
    """Stateful monitor for a single host.

    Tracks both:
      - `raw_status`: the status of the most recent individual ping batch
        (can flap freely, e.g. UP/DEGRADED/DOWN per run()).
      - `effective_status`: a debounced status that only changes after
        `down_threshold`/`up_threshold` consecutive raw results agree,
        which is what alerting logic should key off of.
    """

    def __init__(
        self,
        host: str,
        count: int = 4,
        timeout: float = 1.0,
        interval: float = 0.2,
        down_threshold: int = 2,
        up_threshold: int = 2,
    ) -> None:
        if down_threshold < 1 or up_threshold < 1:
            raise ValueError("down_threshold and up_threshold must be >= 1")

        self.host = host
        self.count = count
        self.timeout = timeout
        self.interval = interval
        self.down_threshold = down_threshold
        self.up_threshold = up_threshold

        self.raw_status: Optional[PingStatus] = None
        self.effective_status: Optional[PingStatus] = None

        # Consecutive counters used purely for threshold evaluation.
        self._consecutive_down = 0
        self._consecutive_up = 0

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run(self) -> PingResult:
        """Execute a single ping batch and update status-change tracking.

        Note: this updates `raw_status` immediately but only updates
        `effective_status` once the configured threshold is met — see
        class docstring.
        """
        result = ping_host(
            host=self.host, count=self.count, timeout=self.timeout, interval=self.interval
        )

        previous_effective = self.effective_status
        self.raw_status = result.status

        self._update_effective_status(result.status)

        result.previous_status = previous_effective
        result.status_changed = previous_effective is not None and previous_effective != self.effective_status
        # Reflect the debounced status as the authoritative one seen by
        # callers, while raw batch status remains available on the
        # instance via `self.raw_status`.
        result.status = self.effective_status or result.status

        return result

    def _update_effective_status(self, raw: PingStatus) -> None:
        """Flapping-protected effective status transition.

        UP counts toward recovery; DOWN and DEGRADED both count toward
        the "not healthy" streak so a host that flaps between DEGRADED
        and DOWN doesn't dodge threshold-based alerting.
        """
        if self.effective_status is None:
            # First observation: seed effective_status directly so
            # monitoring has an immediate baseline instead of waiting
            # for threshold to be met on cold start.
            self.effective_status = raw
            self._consecutive_down = 0 if raw == PingStatus.UP else 1
            self._consecutive_up = 1 if raw == PingStatus.UP else 0
            return

        if raw == PingStatus.UP:
            self._consecutive_up += 1
            self._consecutive_down = 0
            if self.effective_status != PingStatus.UP and self._consecutive_up >= self.up_threshold:
                self.effective_status = PingStatus.UP
        else:
            # DOWN or DEGRADED both count as "not up" for threshold purposes.
            self._consecutive_down += 1
            self._consecutive_up = 0
            if self.effective_status == PingStatus.UP and self._consecutive_down >= self.down_threshold:
                self.effective_status = raw
            elif self.effective_status != PingStatus.UP:
                # Already unhealthy: reflect the latest raw severity
                # immediately (DEGRADED <-> DOWN) without waiting for
                # threshold, since we're already in an alerting state.
                self.effective_status = raw

    def start(
        self,
        monitor_interval: float = 10.0,
        on_result: Optional[Callable[[PingResult], None]] = None,
    ) -> None:
        """Start continuous monitoring on a background thread.

        Pings are fired every `monitor_interval` seconds (measured from
        the start of one run to the start of the next, best-effort).
        Call `stop()` to end monitoring.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Monitor already running")

        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                cycle_start = time.monotonic()
                try:
                    result = self.run()
                    if on_result is not None:
                        on_result(result)
                except Exception:  # noqa: BLE001 - never let the loop die silently
                    logger.exception("Unhandled error during monitoring cycle")

                elapsed = time.monotonic() - cycle_start
                remaining = monitor_interval - elapsed
                if remaining > 0:
                    self._stop_event.wait(remaining)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        """Signal the monitoring loop to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
