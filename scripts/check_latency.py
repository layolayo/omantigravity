#!/usr/bin/env python3
"""
Real-time latency, TTFB, and API health tracker for Google Antigravity / Gemini CLI.
Monitors daily-cloudcode-pa.googleapis.com upstream latency, turn response durations,
and detects 503 capacity exhaustion errors.

Part of the Omantigravity Omarchy plugin suite.
Hardened against Omarchy marketplace criteria (#6127).
"""

from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secure_fs import open_verified_chain, run_sandboxed_binary_fd

DEFAULT_LOG_DIR = Path.home() / ".gemini" / "antigravity-cli" / "log"
DEFAULT_HOST = "daily-cloudcode-pa.googleapis.com"

PING_PATH = Path("/usr/bin/ping")
CURL_PATH = Path("/usr/bin/curl")

# 1 MiB hard ceiling on log read to protect against massive files or unkillable hangs
MAX_LOG_READ_BYTES = 1 * 1024 * 1024

HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)
PROHIBITED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".onion", ".arpa")
LOG_FILENAME_RE = re.compile(r"^cli-[a-zA-Z0-9_\-]+\.log$")


def validate_host(host: str) -> bool:
    """Validate host string against DNS hostname rules, rejecting SSRF/injection vectors."""
    if not isinstance(host, str) or not host:
        return False
    host = host.strip()
    if "@" in host or ":" in host or "/" in host or "\\" in host:
        return False
    if host.startswith("-") or host.endswith("."):
        return False
    if not HOST_RE.match(host):
        return False

    # Reject any IP address (IPv4 or IPv6, loopback, private, or metadata)
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass

    lower = host.lower()
    if lower.endswith(PROHIBITED_HOST_SUFFIXES):
        return False

    parts = lower.split(".")
    if any(p.isdigit() for p in parts):
        return False

    return True


def check_network(host: str, deadline: float) -> Tuple[Optional[float], Optional[float]]:
    """Measure ping RTT and HTTPS TTFB (in milliseconds) via verified root-owned binary fds."""
    if not validate_host(host):
        return None, None

    ping_ms: Optional[float] = None
    if time.monotonic() < deadline:
        ping_fd = open_verified_chain(PING_PATH, want_dir=False, require_root=True)
        if ping_fd is not None:
            try:
                out = run_sandboxed_binary_fd(
                    ping_fd, ["-c", "1", "-W", "1", "--", host], timeout=1.5
                )
                if out:
                    match = re.search(r"rtt min/avg/max/mdev = [\d\.]+/([\d\.]+)/", out)
                    if not match:
                        match = re.search(r"time=([\d\.]+)\s*ms", out)
                    if match:
                        try:
                            ping_ms = round(float(match.group(1)), 1)
                        except ValueError:
                            pass
            finally:
                try:
                    os.close(ping_fd)
                except OSError:
                    pass

    ttfb_ms: Optional[float] = None
    if time.monotonic() < deadline:
        curl_fd = open_verified_chain(CURL_PATH, want_dir=False, require_root=True)
        if curl_fd is not None:
            try:
                out = run_sandboxed_binary_fd(
                    curl_fd,
                    [
                        "-o", "/dev/null", "-s",
                        "-w", "%{time_total}",
                        "--connect-timeout", "1",
                        "--max-time", "2",
                        "--",
                        f"https://{host}"
                    ],
                    timeout=2.0
                )
                if out:
                    try:
                        ttfb_ms = round(float(out.strip()) * 1000.0, 1)
                    except ValueError:
                        pass
            finally:
                try:
                    os.close(curl_fd)
                except OSError:
                    pass

    return ping_ms, ttfb_ms


def find_latest_log_name(dir_fd: int) -> Optional[str]:
    """Find the newest regular cli-*.log file using os.scandir on the verified directory fd."""
    newest_name: Optional[str] = None
    newest_mtime: float = -1.0

    try:
        with os.scandir(dir_fd) as it:
            for entry in it:
                if not LOG_FILENAME_RE.match(entry.name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                        continue
                    if st.st_mtime > newest_mtime:
                        newest_mtime = st.st_mtime
                        newest_name = entry.name
                except OSError:
                    continue
    except OSError:
        return None

    return newest_name


def read_log_tail(dir_fd: int, filename: str) -> Optional[str]:
    """Read at most MAX_LOG_READ_BYTES from the end of the log file without following symlinks.

    Opens with O_NONBLOCK to prevent FIFO hangs, validates S_ISREG before reading,
    and strips any leading partial line.
    """
    file_fd = -1
    try:
        file_fd = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=dir_fd
        )
        st = os.fstat(file_fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None

        # Reset non-blocking mode now that regular file status is verified
        flags = fcntl.fcntl(file_fd, fcntl.F_GETFL)
        fcntl.fcntl(file_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

        file_size = st.st_size
        if file_size <= 0:
            return ""

        to_read = min(file_size, MAX_LOG_READ_BYTES)
        if file_size > MAX_LOG_READ_BYTES:
            os.lseek(file_fd, file_size - to_read, os.SEEK_SET)

        raw_bytes = os.read(file_fd, to_read)
        text = raw_bytes.decode("utf-8", errors="ignore")

        # Discard initial partial line if we sought past byte 0
        if file_size > MAX_LOG_READ_BYTES:
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1:]
            else:
                text = ""

        return text
    except OSError:
        return None
    finally:
        if file_fd != -1:
            try:
                os.close(file_fd)
            except OSError:
                pass


def parse_log_text(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse log text for active turn intervals and sanitized errors."""
    calls: List[Tuple[datetime, float]] = []
    errors: List[Dict[str, Any]] = []

    ts_pattern = re.compile(
        r"^[IWEF](\d{4} \d{2}:\d{2}:\d{2}\.\d{6})\s+\d+\s+http_helpers\.go:\d+\] URL: https://daily-cloudcode-pa\.googleapis\.com/v1internal:streamGenerateContent"
    )
    err_pattern = re.compile(
        r"^[IWEF](\d{4} \d{2}:\d{2}:\d{2}\.\d{6})\s+\d+\s+run\.go:\d+\] Run: attempt \d+ failed \((.*)\)(?:, retrying.*)?$"
    )

    current_year = datetime.now().year
    for line in text.splitlines():
        m = ts_pattern.search(line)
        if m:
            ts_str = m.group(1)
            try:
                dt = datetime.strptime(f"{current_year}{ts_str}", "%Y%m%d %H:%M:%S.%f")
                calls.append((dt, dt.timestamp()))
            except ValueError:
                pass
            continue

        m_err = err_pattern.search(line)
        if m_err:
            ts_str = m_err.group(1)
            try:
                err_dt = datetime.strptime(f"{current_year}{ts_str}", "%Y%m%d %H:%M:%S.%f")
                raw_msg = m_err.group(2).strip()
                # Categorize error without leaking internal paths, traces, or tokens
                if "503" in raw_msg or "No capacity" in raw_msg:
                    clean_msg = "Google servers at capacity (503)"
                    is_503 = True
                elif "429" in raw_msg or "ResourceExhausted" in raw_msg:
                    clean_msg = "Rate limit reached (429)"
                    is_503 = False
                elif "Unavailable" in raw_msg or "connection refused" in raw_msg.lower():
                    clean_msg = "Upstream service unavailable"
                    is_503 = False
                else:
                    clean_msg = "Upstream service error"
                    is_503 = False

                errors.append({
                    "time": err_dt.strftime("%H:%M:%S"),
                    "timestamp": int(err_dt.timestamp()),
                    "message": clean_msg[:128],
                    "is_capacity_error": is_503,
                })
            except ValueError:
                pass

    active_turns: List[Dict[str, Any]] = []
    for i in range(1, len(calls)):
        prev_time, _ = calls[i - 1]
        curr_time, curr_ts = calls[i]
        diff = (curr_time - prev_time).total_seconds()
        # Intervals under 90s correspond to active multi-turn tool calling
        if 0 < diff < 90:
            status = "fast" if diff < 5 else ("moderate" if diff < 15 else "high")
            active_turns.append({
                "time": curr_time.strftime("%H:%M:%S"),
                "timestamp": int(curr_ts),
                "duration_sec": round(diff, 1),
                "status": status,
            })

    return active_turns[-10:], errors[-10:]


def get_latency_report(
    log_dir: Optional[Path] = None,
    host: str = DEFAULT_HOST,
    enable_network: bool = False
) -> Dict[str, Any]:
    """Compile a full structured latency diagnostic report with strict resource and privacy bounds."""
    if not validate_host(host):
        host = DEFAULT_HOST

    deadline = time.monotonic() + 2.5
    actual_log_dir = log_dir or DEFAULT_LOG_DIR
    dir_fd = open_verified_chain(actual_log_dir, want_dir=True, require_root=False)

    turns: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    latest_name: Optional[str] = None

    if dir_fd is not None:
        try:
            latest_name = find_latest_log_name(dir_fd)
            if latest_name:
                log_text = read_log_tail(dir_fd, latest_name)
                if log_text:
                    turns, errors = parse_log_text(log_text)
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass

    ping_ms: Optional[float] = None
    ttfb_ms: Optional[float] = None
    if enable_network:
        ping_ms, ttfb_ms = check_network(host, deadline=deadline)

    recent_turns = turns[-5:]
    avg_turn = (
        round(sum(t["duration_sec"] for t in recent_turns) / len(recent_turns), 1)
        if recent_turns else None
    )

    now_ts = int(datetime.now().timestamp())

    # Mark freshness of errors (active if within the last 5 minutes / 300s)
    recent_errors_list = []
    for err in errors[-5:]:
        err_copy = dict(err)
        err_ts = err_copy.get("timestamp", 0)
        err_copy["is_recent"] = bool(err_ts and (now_ts - err_ts <= 300))
        recent_errors_list.append(err_copy)

    # Determine health state (only active/recent capacity errors cause degraded health)
    has_capacity_issues = any(e.get("is_capacity_error") and e.get("is_recent") for e in recent_errors_list)
    if has_capacity_issues or (avg_turn is not None and avg_turn > 15):
        health = "degraded"
    elif avg_turn is not None and avg_turn > 8:
        health = "slow"
    else:
        health = "healthy"

    return {
        "status": "ok",
        "timestamp": now_ts,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": host,
        "log_file": latest_name,
        "health": health,
        "network": {
            "ping_ms": ping_ms,
            "ttfb_ms": ttfb_ms,
        },
        "average_turn_sec": avg_turn,
        "recent_turns": recent_turns,
        "recent_errors": recent_errors_list,
    }


def print_cli_report(data: Dict[str, Any]) -> None:
    """Format and print the report for terminal CLI viewing without leaking traces."""
    net = data.get("network", {})
    ping = f"{net.get('ping_ms')} ms" if net.get("ping_ms") is not None else "Disabled/N/A"
    ttfb = f"{net.get('ttfb_ms')} ms" if net.get("ttfb_ms") is not None else "Disabled/N/A"
    health = data.get("health", "unknown").upper()
    health_icon = "🟢" if health == "HEALTHY" else ("🟡" if health == "SLOW" else "🔴")

    print(f"\n{health_icon} Antigravity API Health & Latency Report — {data.get('time')}")
    print(f"- Host: `{data.get('host')}`")
    print(f"- Ping: **{ping}** | HTTPS TTFB: **{ttfb}** | Status: **{health}**")

    errors = data.get("recent_errors", [])
    if errors:
        print("\n⚠️  Upstream Capacity & Service Errors:")
        for err in errors[-3:]:
            prefix = "🛑 " if err.get("is_capacity_error") else "⚠️ "
            print(f"  {prefix}[{err['time']}] {err['message']}")

    turns = data.get("recent_turns", [])
    if turns:
        print("\n⚡ Recent Active Turn Durations (excl. user idle waits):")
        for t in turns[-5:]:
            status_tag = "⚡ Fast" if t["status"] == "fast" else ("🟡 Slow" if t["status"] == "moderate" else "🛑 High Latency")
            print(f"  - [{t['time']}] {t['duration_sec']}s ({status_tag})")
    else:
        print("\n- No recent multi-turn activity logged.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check Google Antigravity CLI upstream latency, TTFB, and server capacity status."
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON data")
    parser.add_argument("--watch", type=int, metavar="SEC", help="Continuously poll every SEC seconds")
    parser.add_argument("--network", action="store_true", help="Enable active network ping and TTFB checks")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"API Host to ping/curl (default: {DEFAULT_HOST})")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="Path to Antigravity CLI logs")

    args = parser.parse_args()

    while True:
        report = get_latency_report(
            log_dir=args.log_dir,
            host=args.host,
            enable_network=args.network
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_cli_report(report)

        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
