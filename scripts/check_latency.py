#!/usr/bin/env python3
"""
Real-time latency, TTFB, and API health tracker for Google Antigravity / Gemini CLI.
Monitors daily-cloudcode-pa.googleapis.com upstream latency, turn response durations,
and detects 503 capacity exhaustion errors.

Part of the Omantigravity Omarchy plugin suite.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_LOG_DIR = Path.home() / ".gemini" / "antigravity-cli" / "log"
DEFAULT_HOST = "daily-cloudcode-pa.googleapis.com"


def get_latest_log(log_dir: Path) -> Optional[Path]:
    """Find the most recently modified cli-*.log file."""
    pattern = str(log_dir / "cli-*.log")
    logs = glob.glob(pattern)
    if not logs:
        return None
    logs.sort(key=os.path.getmtime, reverse=True)
    return Path(logs[0])


def check_network(host: str) -> Tuple[Optional[float], Optional[float]]:
    """Measure ping RTT and HTTPS TTFB (in milliseconds)."""
    ping_ms: Optional[float] = None
    try:
        out = subprocess.check_output(
            ["ping", "-c", "2", "-W", "2", host],
            stderr=subprocess.DEVNULL
        ).decode()
        match = re.search(r"rtt min/avg/max/mdev = [\d\.]+/([\d\.]+)/", out)
        if match:
            ping_ms = round(float(match.group(1)), 1)
    except Exception:
        pass

    ttfb_ms: Optional[float] = None
    try:
        out = subprocess.check_output([
            "curl", "-o", "/dev/null", "-s",
            "-w", "%{time_total}",
            "--connect-timeout", "3",
            "--max-time", "5",
            f"https://{host}"
        ], stderr=subprocess.DEVNULL).decode().strip()
        ttfb_ms = round(float(out) * 1000.0, 1)
    except Exception:
        pass

    return ping_ms, ttfb_ms


def parse_log(log_path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse log for streamGenerateContent calls and 503 capacity / run errors."""
    calls: List[Tuple[datetime, str, str]] = []
    errors: List[Dict[str, Any]] = []

    ts_pattern = re.compile(
        r"^[IWEF](\d{4} \d{2}:\d{2}:\d{2}\.\d{6})\s+\d+\s+http_helpers\.go:\d+\] URL: https://daily-cloudcode-pa\.googleapis\.com/v1internal:streamGenerateContent"
    )
    err_pattern = re.compile(
        r"^[IWEF](\d{4} \d{2}:\d{2}:\d{2}\.\d{6})\s+\d+\s+run\.go:\d+\] Run: attempt \d+ failed \((.*)\)(?:, retrying.*)?$"
    )

    current_year = datetime.now().year
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = ts_pattern.search(line)
                if m:
                    ts_str = m.group(1)
                    dt = datetime.strptime(f"{current_year}{ts_str}", "%Y%m%d %H:%M:%S.%f")
                    m_resp = re.search(r"ResponseID: (\S+)", line)
                    resp_id = m_resp.group(1) if m_resp else "unknown"
                    m_trace = re.search(r"Trace: (\S+)", line)
                    trace_id = m_trace.group(1) if m_trace else "unknown"
                    calls.append((dt, resp_id, trace_id))

                m_err = err_pattern.search(line)
                if m_err:
                    ts_str = m_err.group(1)
                    err_dt = datetime.strptime(f"{current_year}{ts_str}", "%Y%m%d %H:%M:%S.%f")
                    err_msg = m_err.group(2).strip()
                    is_503 = "503" in err_msg or "No capacity" in err_msg
                    errors.append({
                        "time": err_dt.strftime("%H:%M:%S"),
                        "timestamp": int(err_dt.timestamp()),
                        "message": err_msg,
                        "is_capacity_error": is_503,
                    })
    except Exception:
        pass

    active_turns: List[Dict[str, Any]] = []
    for i in range(1, len(calls)):
        prev_time, _, _ = calls[i - 1]
        curr_time, resp_id, trace_id = calls[i]
        diff = (curr_time - prev_time).total_seconds()
        # Intervals under 90s correspond to active multi-turn tool calling
        if diff < 90:
            status = "fast" if diff < 5 else ("moderate" if diff < 15 else "high")
            active_turns.append({
                "time": curr_time.strftime("%H:%M:%S"),
                "timestamp": int(curr_time.timestamp()),
                "duration_sec": round(diff, 1),
                "status": status,
                "response_id": resp_id,
                "trace_id": trace_id,
            })

    return active_turns, errors


def get_latency_report(log_dir: Path = DEFAULT_LOG_DIR, host: str = DEFAULT_HOST) -> Dict[str, Any]:
    """Compile a full structured latency diagnostic report."""
    latest_log = get_latest_log(log_dir)
    ping_ms, ttfb_ms = check_network(host)
    turns, errors = parse_log(latest_log) if latest_log else ([], [])

    recent_turns = turns[-5:]
    avg_turn = round(sum(t["duration_sec"] for t in recent_turns) / len(recent_turns), 1) if recent_turns else None

    # Determine health state
    has_capacity_issues = any(e.get("is_capacity_error") for e in errors[-5:])
    if has_capacity_issues or (avg_turn is not None and avg_turn > 15):
        health = "degraded"
    elif avg_turn is not None and avg_turn > 8:
        health = "slow"
    else:
        health = "healthy"

    return {
        "status": "ok",
        "timestamp": int(datetime.now().timestamp()),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": host,
        "log_file": str(latest_log) if latest_log else None,
        "health": health,
        "network": {
            "ping_ms": ping_ms,
            "ttfb_ms": ttfb_ms,
        },
        "average_turn_sec": avg_turn,
        "recent_turns": recent_turns,
        "recent_errors": errors[-5:],
    }


def print_cli_report(data: Dict[str, Any]) -> None:
    """Format and print the report for terminal CLI viewing."""
    net = data.get("network", {})
    ping = f"{net.get('ping_ms')} ms" if net.get("ping_ms") is not None else "N/A"
    ttfb = f"{net.get('ttfb_ms')} ms" if net.get("ttfb_ms") is not None else "N/A"
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
            print(f"  - [{t['time']}] {t['duration_sec']}s ({status_tag}) | Trace: {t['trace_id']} | Resp: {t['response_id']}")
    else:
        print("\n- No recent multi-turn activity logged.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check Google Antigravity CLI upstream latency, TTFB, and server capacity status."
    )
    parser.add_argument("--json", action="store_true", help="Output raw JSON data")
    parser.add_argument("--watch", type=int, metavar="SEC", help="Continuously poll every SEC seconds")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"API Host to ping/curl (default: {DEFAULT_HOST})")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="Path to Antigravity CLI logs")

    args = parser.parse_args()

    while True:
        report = get_latency_report(log_dir=args.log_dir, host=args.host)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_cli_report(report)

        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
