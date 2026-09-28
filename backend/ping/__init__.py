"""Python ICMP Ping Monitoring Engine.

Public API:
    ping_host(host, count, timeout, interval) -> PingResult
    PingMonitor(host, ...) -> stateful monitor with flapping protection
"""

from .exceptions import (
    DnsResolutionError,
    InvalidHostError,
    PingExecutableNotFoundError,
    PingMonitorError,
    PingPermissionError,
)
from .models import LatencyStats, PingReply, PingResult, PingStatus
from .ping_monitor import PingMonitor, ping_host

__all__ = [
    "ping_host",
    "PingMonitor",
    "PingResult",
    "PingReply",
    "LatencyStats",
    "PingStatus",
    "PingMonitorError",
    "InvalidHostError",
    "DnsResolutionError",
    "PingExecutableNotFoundError",
    "PingPermissionError",
]
