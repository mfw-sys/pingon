"""Custom exceptions used across the ping monitoring engine.

These are intentionally narrow so callers (e.g. a REST API layer) can
map them to precise HTTP error responses instead of a generic 500.
"""

from __future__ import annotations


class PingMonitorError(Exception):
    """Base class for all ping-monitor specific errors."""


class InvalidHostError(PingMonitorError):
    """Raised when the given host string is syntactically invalid.

    This is NOT raised for hosts that are simply unreachable — only for
    hosts that could never be valid (empty string, illegal characters,
    malformed IP literal, etc). Unreachable-but-valid hosts should
    result in a DOWN status, not an exception.
    """


class DnsResolutionError(PingMonitorError):
    """Raised when a hostname cannot be resolved to an IP address."""

    def __init__(self, host: str, original_error: Exception | None = None) -> None:
        self.host = host
        self.original_error = original_error
        super().__init__(f"DNS resolution failed for host: {host!r}")


class PingExecutableNotFoundError(PingMonitorError):
    """Raised when the system 'ping' binary cannot be located."""

    def __init__(self) -> None:
        super().__init__("ICMP ping executable not found")


class PingPermissionError(PingMonitorError):
    """Raised when the OS denies permission to send ICMP packets.

    On Linux this can happen when ping is not setuid/setcap and the
    process lacks CAP_NET_RAW. On some hardened environments this
    manifests as a non-zero exit code with a permission-denied message
    rather than an OSError, so ping_engine inspects stderr as well.
    """
