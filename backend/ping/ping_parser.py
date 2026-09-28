"""Parses native `ping` command output into structured reply data.

Design note (see section 11 of the spec / README): latency values here
are read directly from the RTT that the native ping binary itself
reports (the `time=` field), never derived from Python-side
`perf_counter()` wall-clock around the subprocess call. This avoids
polluting RTT with DNS resolution, process startup, or output-parsing
overhead — the value that reaches the caller is exactly the round trip
time the OS's ICMP stack measured.

Reply order (not the OS's own icmp_seq numbering, since Windows doesn't
print one) is used to assign `sequence`, because that numbering is only
used for display/logging and must always be 1..count regardless of
platform quirks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- Regex patterns ---------------------------------------------------
#
# Linux (iputils) and macOS (BSD ping) share the same reply line shape:
#   "64 bytes from 8.8.8.8: icmp_seq=1 ttl=113 time=20.4 ms"
# Timeout lines differ slightly but both contain "timeout" or similar.
_UNIX_REPLY_RE = re.compile(
    r"bytes from .*?time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE
)
_UNIX_TIMEOUT_RE = re.compile(
    r"(request timeout for icmp_seq|no answer yet)", re.IGNORECASE
)

# Windows:
#   "Reply from 8.8.8.8: bytes=32 time=20ms TTL=113"
#   "Reply from 8.8.8.8: bytes=32 time<1ms TTL=113"
_WINDOWS_REPLY_RE = re.compile(
    r"reply from .*?time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE
)
_WINDOWS_TIMEOUT_RE = re.compile(
    r"(request timed out|destination host unreachable|"
    r"destination net unreachable|general failure|"
    r"transmit failed)",
    re.IGNORECASE,
)


@dataclass
class ParsedReply:
    success: bool
    latency_ms: float | None


def parse_ping_output(raw_output: str, is_windows: bool) -> list[ParsedReply]:
    """Parse raw stdout from a native ping invocation into ParsedReply items.

    Only lines that unambiguously represent either a successful reply or
    a timed-out/unreachable request are counted. Header lines (e.g.
    "PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.") and summary/footer
    lines (e.g. "4 packets transmitted, ...") are intentionally ignored
    since they carry no per-packet information.

    Args:
        raw_output: combined stdout of the ping subprocess.
        is_windows: whether the output came from Windows' ping.exe.

    Returns:
        List of ParsedReply in the order the packets were reported.
    """
    reply_re = _WINDOWS_REPLY_RE if is_windows else _UNIX_REPLY_RE
    timeout_re = _WINDOWS_TIMEOUT_RE if is_windows else _UNIX_TIMEOUT_RE

    results: list[ParsedReply] = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue

        reply_match = reply_re.search(line)
        if reply_match:
            latency = float(reply_match.group(1))
            results.append(ParsedReply(success=True, latency_ms=latency))
            continue

        if timeout_re.search(line):
            results.append(ParsedReply(success=False, latency_ms=None))
            continue

        # Any other line (headers, statistics summary, blank noise) is
        # deliberately skipped — it carries no per-packet signal.

    return results
