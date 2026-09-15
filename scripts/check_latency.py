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
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_LOG_DIR = Path.home() / ".gemini" / "antigravity-cli" / "log"
DEFAULT_HOST = "daily-cloudcode-pa.googleapis.com"

PING_BIN = "/usr/bin/ping"
CURL_BIN = "/usr/bin/curl"

# 1 MiB hard ceiling on log read to protect against massive files or unkillable hangs
MAX_LOG_READ_BYTES = 1 * 1024 * 1024

HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)
PROHIBITED_HOST_PREFIXES = (
    "127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
    "0.0.0.0", "::1", "fc", "fd", "fe80"
)
PROHIBITED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".onion", ".arpa")

LOG_FILENAME_RE = re.compile(r"^cli-[a-zA-Z0-9_\-]+\.log$")


def validate_host(host: str) -> bool:
    """Validate host string against DNS hostname rules, rejecting SSRF/injection vectors."""
    if not isinstance(host, str) or not host:
        return False
    host = host.strip().lower()
    if not HOST_RE.match(host):
        return False
    if host.startswith(PROHIBITED_HOST_PREFIXES):
        return False
    if host.endswith(PROHIBITED_HOST_SUFFIXES):
        return False
    tld = host.split(".")[-1]
    if tld.isdigit():
        return False
    return True


def _verify_system_binary(path_str: str) -> bool:
    """Verify that path_str is an absolute, root/user-owned, non-group/world-writable executable regular file."""
    try:
        st = os.stat(path_str, follow_symlinks=False)
        return (
            stat.S_ISREG(st.st_mode)
            and st.st_uid in (0, os.getuid())
            and not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
            and (st.st_mode & stat.S_IXUSR) != 0
        )
    except OSError:
        return False


def _run_sandboxed_cmd(cmd: List[str], timeout: float = 3.5) -> Optional[str]:
    """Execute a system binary within a closed environment and dedicated process group."""
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"LC_ALL": "C"},
            start_new_session=True,
        )
        stdout_b, _ = proc.communicate(timeout=timeout)
        if proc.returncode == 0:
            return stdout_b.decode("utf-8", errors="replace")
    except Exception:
        pass
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
    return None


def check_network(host: str) -> Tuple[Optional[float], Optional[float]]:
    """Measure ping RTT and HTTPS TTFB (in milliseconds) via pinned, verified binaries."""
    if not validate_host(host):
        return None, None

    ping_ms: Optional[float] = None
    if _verify_system_binary(PING_BIN):
        out = _run_sandboxed_cmd([PING_BIN, "-c", "2", "-W", "2", "--", host], timeout=3.5)
        if out:
            match = re.search(r"rtt min/avg/max/mdev = [\d\.]+/([\d\.]+)/", out)
            if match:
                try:
                    ping_ms = round(float(match.group(1)), 1)
                except ValueError:
                    pass

    ttfb_ms: Optional[float] = None
    if _verify_system_binary(CURL_BIN):
        out = _run_sandboxed_cmd([
            CURL_BIN, "-o", "/dev/null", "-s",
            "-w", "%{time_total}",
            "--connect-timeout", "2",
            "--max-time", "3",
            "--",
            f"https://{host}"
        ], timeout=3.5)
        if out:
            try:
                ttfb_ms = round(float(out.strip()) * 1000.0, 1)
            except ValueError:
                pass

    return ping_ms, ttfb_ms


def _open_log_dir_fd(log_dir: Path) -> Optional[int]:
    """Open log_dir component by component with O_NOFOLLOW, verifying ownership and mode."""
    try:
        resolved = log_dir.resolve(strict=True)
        if not resolved.is_absolute():
            return None
        parts = resolved.parts[1:]
        if not parts:
            return None
        fd = os.open("/", os.O_DIRECTORY | os.O_CLOEXEC)
        for part in parts:
            flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                os.close(fd)
                return None
            if st.st_uid not in (0, os.getuid()):
                os.close(fd)
                return None
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                os.close(fd)
                return None
        return fd
    except Exception:
        return None


def find_latest_log_name(dir_fd: int) -> Optional[str]:
    """Find the most recently modified cli-*.log file name in the verified dir fd."""
    newest_name: Optional[str] = None
    newest_mtime: float = -1.0

    try:
        for name in os.listdir(dir_fd):
            if not LOG_FILENAME_RE.match(name):
                continue
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_uid != os.getuid():
                    continue
                if st.st_mtime > newest_mtime:
                    newest_mtime = st.st_mtime
                    newest_name = name
            except OSError:
                continue
    except OSError:
        return None

    return newest_name


def read_log_tail(dir_fd: int, filename: str) -> Optional[str]:
    """Read at most MAX_LOG_READ_BYTES from the end of the log file without following symlinks."""
    fd = -1
    try:
        fd = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=dir_fd
        )
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None

        file_size = st.st_size
        if file_size <= 0:
            return ""

        to_read = min(file_size, MAX_LOG_READ_BYTES)
        if file_size > MAX_LOG_READ_BYTES:
            os.lseek(fd, file_size - to_read, os.SEEK_SET)

        raw_bytes = os.read(fd, to_read)
        text = raw_bytes.decode("utf-8", errors="ignore")

        # Discard the initial partial line if we did not read from byte 0
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
        if fd != -1:
            try:
                os.close(fd)
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

    actual_log_dir = log_dir or DEFAULT_LOG_DIR
    dir_fd = _open_log_dir_fd(actual_log_dir)

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
        ping_ms, ttfb_ms = check_network(host)

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
    """Format and print the report for terminal CLI viewing."""
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
