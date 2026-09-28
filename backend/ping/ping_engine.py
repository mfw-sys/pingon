"""Executes native ICMP ping and returns parsed, accurate results.

This module implements Approach A from the design doc: shell out to the
OS's own `ping` binary and parse its output, rather than crafting raw
ICMP packets in Python. See module docstring in ping_parser.py for why
RTT is always taken from the native ping's own `time=` field.
"""

from __future__ import annotations

import ipaddress
import math
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

from .exceptions import (
    DnsResolutionError,
    InvalidHostError,
    PingExecutableNotFoundError,
    PingPermissionError,
)
from .logger import get_logger
from .ping_parser import ParsedReply, parse_ping_output

logger = get_logger()

# Loose hostname validation (RFC 1123-ish): labels of alnum/hyphen,
# joined by dots, no leading/trailing hyphen per label, no illegal
# characters such as '#', spaces, underscores handled leniently.
_HOSTNAME_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


@dataclass
class RawPingExecution:
    """Result of invoking the native ping binary once."""

    replies: list[ParsedReply]
    stderr: str
    return_code: int


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def is_macos() -> bool:
    return platform.system().lower() == "darwin"


def validate_host(host: str) -> None:
    """Raise InvalidHostError if `host` is not a syntactically valid
    IPv4/IPv6 literal or hostname. Does NOT perform any network I/O.
    """
    if not host or not host.strip():
        raise InvalidHostError("Host must not be empty")

    host = host.strip()

    # Valid IP literal short-circuits hostname validation entirely.
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass

    if len(host) > 253:
        raise InvalidHostError(f"Host too long: {host!r}")

    labels = host.rstrip(".").split(".")
    if not labels or not all(_HOSTNAME_LABEL_RE.match(label) for label in labels):
        raise InvalidHostError(f"Invalid host format: {host!r}")


def resolve_host(host: str) -> str:
    """Resolve a hostname to an IP address string. If `host` is already
    an IP literal, returns it unchanged without a DNS lookup.
    """
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    try:
        # getaddrinfo works for both IPv4 and IPv6 and respects the
        # system resolver / hosts file, unlike gethostbyname.
        infos = socket.getaddrinfo(host, None)
        return infos[0][4][0]
    except socket.gaierror as exc:
        raise DnsResolutionError(host, original_error=exc) from exc


def _find_ping_binary() -> str:
    binary = shutil.which("ping")
    if binary is None:
        raise PingExecutableNotFoundError()
    return binary


def _build_command(
    host: str, count: int, timeout: float, interval: float
) -> list[str]:
    """Build the OS-appropriate ping command for a *single subprocess
    call* covering the whole batch (used on Linux/macOS, which support
    a native interval flag). Windows is handled per-packet by the
    caller since ping.exe has no interval flag.
    """
    binary = _find_ping_binary()

    if is_windows():
        # -n count, -w timeout(ms). No interval support.
        timeout_ms = max(1, int(timeout * 1000))
        return [binary, "-n", str(count), "-w", str(timeout_ms), host]

    if is_macos():
        # BSD ping: -c count, -i interval(s), -W waittime(ms) per reply.
        timeout_ms = max(1, int(timeout * 1000))
        return [
            binary,
            "-c",
            str(count),
            "-i",
            f"{interval:.3f}",
            "-W",
            str(timeout_ms),
            host,
        ]

    # Linux (iputils): -c count, -i interval(s, min 0.2 unprivileged),
    # -W timeout(s, applies per-reply in modern iputils).
    timeout_s = max(1, math.ceil(timeout))
    return [
        binary,
        "-c",
        str(count),
        "-i",
        f"{max(interval, 0.2):.3f}",
        "-W",
        str(timeout_s),
        host,
    ]


def _run_subprocess(cmd: list[str], overall_timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=overall_timeout,
        )
    except FileNotFoundError as exc:
        raise PingExecutableNotFoundError() from exc
    except subprocess.TimeoutExpired as exc:
        # The whole batch subprocess hung past our safety margin. Treat
        # remaining packets as lost rather than raising — this is a
        # monitoring signal (DOWN), not an application error.
        logger.warning("Ping subprocess exceeded overall timeout: %s", exc)
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(exc))


def _check_permission_error(stderr: str, return_code: int) -> None:
    lowered = stderr.lower()
    permission_markers = (
        "operation not permitted",
        "permission denied",
        "must run as root",
        "requires elevated privileges",
        "socket: not permitted",
    )
    if any(marker in lowered for marker in permission_markers):
        raise PingPermissionError(
            "Permission denied while sending ICMP packets. "
            "On Linux, ensure the ping binary has CAP_NET_RAW "
            "(setcap cap_net_raw+ep $(which ping)) or run with sufficient "
            "privileges. On Windows, try running as Administrator."
        )


def execute_native_ping(
    host: str, count: int, timeout: float, interval: float
) -> RawPingExecution:
    """Execute native ping against `host` and return parsed replies.

    On Linux/macOS this issues a single subprocess call for the whole
    batch (the OS's own ping handles spacing via -i). On Windows,
    ping.exe has no interval flag, so we issue `count` single-packet
    calls, sleeping `interval` seconds between them (never after the
    last one), which reproduces the same timing semantics.
    """
    if is_windows():
        return _execute_windows_per_packet(host, count, timeout, interval)

    cmd = _build_command(host, count, timeout, interval)
    # Safety margin so a hung subprocess can't block monitoring forever.
    overall_timeout = count * (timeout + interval) + 5.0

    logger.debug("Running command: %s", " ".join(cmd))
    completed = _run_subprocess(cmd, overall_timeout)

    _check_permission_error(completed.stderr, completed.returncode)

    replies = parse_ping_output(completed.stdout, is_windows=False)
    return RawPingExecution(
        replies=replies, stderr=completed.stderr, return_code=completed.returncode
    )


def _execute_windows_per_packet(
    host: str, count: int, timeout: float, interval: float
) -> RawPingExecution:
    binary = _find_ping_binary()
    timeout_ms = max(1, int(timeout * 1000))

    replies: list[ParsedReply] = []
    combined_stderr: list[str] = []
    last_return_code = 0

    for seq in range(count):
        cmd = [binary, "-n", "1", "-w", str(timeout_ms), host]
        logger.debug("Running command: %s", " ".join(cmd))
        completed = _run_subprocess(cmd, timeout + 5.0)

        _check_permission_error(completed.stderr, completed.returncode)

        parsed = parse_ping_output(completed.stdout, is_windows=True)
        if parsed:
            replies.append(parsed[0])
        else:
            replies.append(ParsedReply(success=False, latency_ms=None))

        if completed.stderr:
            combined_stderr.append(completed.stderr)
        last_return_code = completed.returncode

        is_last = seq == count - 1
        if not is_last and interval > 0:
            time.sleep(interval)

    return RawPingExecution(
        replies=replies,
        stderr="\n".join(combined_stderr),
        return_code=last_return_code,
    )
