"""Result data structures for the ping monitoring engine.

All structures are dataclasses so they serialize cleanly to dict/JSON
for use in a REST API layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class PingStatus(str, Enum):
    """Status of a single ping execution (a batch of `count` packets)."""

    UP = "UP"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


@dataclass
class PingReply:
    """Result of a single ICMP echo request within a ping batch."""

    sequence: int
    success: bool
    latency_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LatencyStats:
    """Aggregated latency statistics computed from successful replies only."""

    min: Optional[float] = None
    max: Optional[float] = None
    avg: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PingResult:
    """Full result of one ping_host() / PingMonitor.run() execution."""

    host: str
    resolved_ip: Optional[str]
    status: PingStatus
    sent: int
    received: int
    packet_loss: float
    latency: LatencyStats
    replies: list[PingReply] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).astimezone().isoformat()
    )
    error: Optional[str] = None
    previous_status: Optional[PingStatus] = None
    status_changed: Optional[bool] = None

    def to_dict(self) -> dict:
        data = {
            "host": self.host,
            "resolved_ip": self.resolved_ip,
            "status": self.status.value,
            "sent": self.sent,
            "received": self.received,
            "packet_loss": self.packet_loss,
            "latency": self.latency.to_dict(),
            "replies": [r.to_dict() for r in self.replies],
            "timestamp": self.timestamp,
        }
        if self.error is not None:
            data["error"] = self.error
        if self.previous_status is not None:
            data["previous_status"] = self.previous_status.value
        if self.status_changed is not None:
            data["status_changed"] = self.status_changed
        return data
