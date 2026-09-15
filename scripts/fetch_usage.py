#!/usr/bin/env python3
"""
Fetch, parse, and cache Antigravity CLI usage limits and quota information.
Designed for the Omarchy Omantigravity bar widget & panel plugin.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secure_fs import open_verified_chain, owner_and_mode_ok
try:
    from check_latency import get_latency_report
except Exception:
    get_latency_report = None

# Hard cap on combined stdout/stderr read from the `agy` CLI, to bound memory
# use if the process is hostile or wedged and keeps producing output.
MAX_OUTPUT_BYTES = 2 * 1024 * 1024  # 2 MiB

# Hard cap on the cached usage JSON we will read back, so a huge or
# never-ending cache file (e.g. a FIFO) can't exhaust the helper's memory.
CACHE_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB

CACHE_FILENAME = "antigravity-usage.json"

# Environment variable fallback for the explicitly configured agy path, used
# when this script isn't invoked with --agy-path (e.g. manual/CLI use).
AGY_PATH_ENV_VAR = "OMANTIGRAVITY_AGY_PATH"


def find_agy_binary(configured_path: Optional[str]) -> Optional[int]:
    """Return an open, verified file descriptor for the `agy` binary, or
    None. Callers must close the fd when done.

    There is deliberately no automatic discovery here (no PATH search, no
    shutil.which, no guessing at mise/shim/home install locations). A file
    merely sitting at a conventional path and being owned/executable by the
    current user proves nothing about *what* it is -- it could be an
    unrelated or substituted program, and silently executing it would hand
    over the user's AI session/credentials without their knowledge.

    Instead we only ever execute the exact path the user explicitly
    configured (the plugin's "agyPath" setting, passed in as
    `configured_path`, or the OMANTIGRAVITY_AGY_PATH env var as a CLI-only
    fallback). That explicit choice is the provenance/consent boundary; the
    fd-chain verification below is defense in depth on top of it, not a
    substitute for it, so a configured path that fails those checks is
    still rejected.
    """
    raw = configured_path or os.environ.get(AGY_PATH_ENV_VAR)
    if not raw:
        return None

    if any(c in raw for c in ("$", "\0", "\n", "\r")):
        return None

    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        return None

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    return open_verified_chain(resolved, want_dir=False, require_root=False)


def _read_capped(proc: subprocess.Popen, timeout: float, max_bytes: int) -> tuple[bytes, bool]:
    """Read proc.stdout/stderr with a byte cap and overall deadline.

    Returns (stdout_bytes, truncated). Never raises on timeout/overflow;
    the caller is responsible for killing the process group afterwards.
    """
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    open_streams = {"stdout", "stderr"}
    deadline = time.monotonic() + timeout
    truncated = False

    while open_streams:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            truncated = True
            break
        for key, _ in sel.select(timeout=min(remaining, 0.5)):
            name = key.data
            try:
                chunk = os.read(key.fileobj.fileno(), 65536)
            except OSError:
                chunk = b""
            if not chunk:
                sel.unregister(key.fileobj)
                open_streams.discard(name)
                continue
            buffers[name].extend(chunk)
            if len(buffers[name]) > max_bytes:
                truncated = True
                open_streams.clear()
                break

    return bytes(buffers["stdout"][:max_bytes]), truncated


def run_agy_command(
    agy_fd: int, cmd: str, timeout: int = 15, max_bytes: int = MAX_OUTPUT_BYTES
) -> Optional[Dict[str, Any]]:
    """Execute the already-open, verified binary fd `agy_fd` via its magic
    /proc/self/fd symlink. Using the fd itself (kept alive across fork+exec
    with pass_fds) rather than a pathname means the kernel executes exactly
    the inode that was verified -- nothing is re-resolved by name at exec
    time, so a swap of any path component after verification has no effect.
    """
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [f"/proc/self/fd/{agy_fd}", "-p", cmd, "--output-format", "json"],
            executable=f"/proc/self/fd/{agy_fd}",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group, for clean teardown
            pass_fds=(agy_fd,),
        )
        stdout_b, truncated = _read_capped(proc, timeout, max_bytes)
        if truncated:
            return None
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return None
        if returncode == 0 and stdout_b.strip():
            return json.loads(stdout_b.decode("utf-8", errors="replace"))
    except Exception:
        pass
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    return None


def get_cache_dir_fd() -> Optional[int]:
    """Create (if needed) and open the cache directory as a verified
    directory file descriptor.

    Every path component from the filesystem root down is opened with
    `openat(..., O_NOFOLLOW)` and checked on its own fd -- never via a
    pathname stat -- to be a real directory owned by root or the current
    user. The final component is additionally required to be mode 0700
    (tightened in place via `fchmod` on the retained fd if it drifted). All
    later cache reads/writes happen relative to this fd via `dir_fd=`, so a
    symlink swapped in anywhere in the chain, or at the final cache
    directory itself, is never followed.
    """
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "omarchy"
    if not base.is_absolute():
        return None

    parts = base.parts[1:]
    fd = os.open("/", os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError:
                if is_last:
                    return None
            try:
                next_fd = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError:
                return None
            os.close(fd)
            fd = next_fd

            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                return None
            if st.st_uid not in (0, os.getuid()):
                return None
            if is_last:
                if st.st_uid != os.getuid():
                    return None
                if stat.S_IMODE(st.st_mode) != 0o700:
                    try:
                        os.fchmod(fd, 0o700)
                    except OSError:
                        return None
            elif st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                return None
        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def sanitize_cache_payload(data: Any) -> Optional[Dict[str, Any]]:
    """Sanitize and bound cached data on read to prevent cache poisoning or trace leakage."""
    if not isinstance(data, dict):
        return None

    # Redact / bound groups and buckets
    raw_groups = data.get("groups", [])
    if isinstance(raw_groups, list):
        clean_groups = []
        for grp in raw_groups[:10]:
            if not isinstance(grp, dict):
                continue
            raw_buckets = grp.get("buckets", [])
            clean_buckets = []
            if isinstance(raw_buckets, list):
                for b in raw_buckets[:10]:
                    if isinstance(b, dict):
                        clean_buckets.append({
                            "id": str(b.get("id", ""))[:64],
                            "name": str(b.get("name", "Limit"))[:64],
                            "window": str(b.get("window", ""))[:32],
                            "window_title": str(b.get("window_title", ""))[:64],
                            "description": str(b.get("description", ""))[:256],
                            "remaining_fraction": float(b.get("remaining_fraction", 1.0)),
                            "remaining_pct": int(b.get("remaining_pct", 100)),
                            "used_pct": int(b.get("used_pct", 0)),
                            "reset_time": str(b.get("reset_time", ""))[:64],
                            "reset_countdown": str(b.get("reset_countdown", ""))[:32],
                            "reset_local": str(b.get("reset_local", ""))[:32],
                            "reset_exact": str(b.get("reset_exact", ""))[:64],
                            "alarming": bool(b.get("alarming", False)),
                            "warning": bool(b.get("warning", False)),
                        })
            clean_groups.append({
                "name": str(grp.get("name", "Unknown Group"))[:64],
                "description": str(grp.get("description", ""))[:256],
                "icon": str(grp.get("icon", "󰘧"))[:8],
                "is_gemini": bool(grp.get("is_gemini", False)),
                "buckets": clean_buckets,
            })
        data["groups"] = clean_groups

    # Redact / bound latency
    lat = data.get("latency")
    if isinstance(lat, dict):
        clean_lat: Dict[str, Any] = {
            "status": str(lat.get("status", "ok"))[:16],
            "timestamp": int(lat.get("timestamp", 0)),
            "time": str(lat.get("time", ""))[:32],
            "host": "daily-cloudcode-pa.googleapis.com",
            "health": str(lat.get("health", "healthy"))[:16],
            "average_turn_sec": lat.get("average_turn_sec"),
            "network": {
                "ping_ms": lat.get("network", {}).get("ping_ms") if isinstance(lat.get("network"), dict) else None,
                "ttfb_ms": lat.get("network", {}).get("ttfb_ms") if isinstance(lat.get("network"), dict) else None,
            },
        }
        clean_turns = []
        for t in lat.get("recent_turns", [])[:5]:
            if isinstance(t, dict):
                clean_turns.append({
                    "time": str(t.get("time", ""))[:16],
                    "timestamp": int(t.get("timestamp", 0)),
                    "duration_sec": float(t.get("duration_sec", 0.0)),
                    "status": str(t.get("status", "fast"))[:16],
                })
        clean_lat["recent_turns"] = clean_turns

        clean_errs = []
        for e in lat.get("recent_errors", [])[:5]:
            if isinstance(e, dict):
                clean_errs.append({
                    "time": str(e.get("time", ""))[:16],
                    "timestamp": int(e.get("timestamp", 0)),
                    "message": str(e.get("message", "Service error"))[:128],
                    "is_capacity_error": bool(e.get("is_capacity_error", False)),
                    "is_recent": bool(e.get("is_recent", False)),
                })
        clean_lat["recent_errors"] = clean_errs
        data["latency"] = clean_lat

    return data


def read_cache(cache_dir_fd: int) -> Optional[Dict[str, Any]]:
    """Read the cache file relative to the verified cache directory fd,
    without following symlinks, with a size cap and sanitization."""
    try:
        fd = os.open(
            CACHE_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cache_dir_fd
        )
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_uid != os.getuid():
            return None
        if st.st_size > CACHE_MAX_BYTES:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = -1  # ownership transferred to the file object
            return sanitize_cache_payload(json.load(f))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if fd != -1:
            os.close(fd)


def write_cache(cache_dir_fd: int, data: Dict[str, Any]) -> None:
    """Write the cache atomically via an exclusive, randomly named temp file
    created relative to the verified cache directory fd, then rename it into
    place relative to that same fd. Using `dir_fd` for both the create and
    the rename means neither step ever resolves a pathname through the
    (potentially attacker-influenced) filesystem namespace -- only through
    the directory fd we already verified."""
    tmp_name = f".antigravity-usage-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=cache_dir_fd,
        )
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, CACHE_FILENAME, src_dir_fd=cache_dir_fd, dst_dir_fd=cache_dir_fd)
    except OSError:
        try:
            os.unlink(tmp_name, dir_fd=cache_dir_fd)
        except OSError:
            pass


def format_countdown(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        clean_iso = iso_str.replace("Z", "+00:00")
        target_dt = datetime.fromisoformat(clean_iso)
        now = datetime.now(timezone.utc)
        diff = target_dt - now
        total_seconds = int(diff.total_seconds())
        if total_seconds <= 0:
            return "ready"

        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60

        if days > 0:
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{max(1, minutes)}m"
    except Exception:
        return ""


def format_local_time(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        clean_iso = iso_str.replace("Z", "+00:00")
        target_dt = datetime.fromisoformat(clean_iso)
        local_dt = target_dt.astimezone()
        return local_dt.strftime("%b %d, %H:%M")
    except Exception:
        return iso_str


def format_exact_time(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        clean_iso = iso_str.replace("Z", "+00:00")
        target_dt = datetime.fromisoformat(clean_iso)
        local_dt = target_dt.astimezone()
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        month_abbr = months[local_dt.month - 1]
        time_part = local_dt.strftime("%H:%M")
        return f"{local_dt.day}, {month_abbr} at {time_part}"
    except Exception:
        return ""


def parse_usage_data(
    usage_raw: Dict[str, Any],
    model_raw: Optional[Dict[str, Any]] = None,
    latency_raw: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    cmd_data = usage_raw.get("command", {}).get("data", {})
    raw_groups = cmd_data.get("groups", [])[:10]
    general_desc = str(cmd_data.get("description", ""))[:256]

    parsed_groups: List[Dict[str, Any]] = []
    lowest_pct = 100
    lowest_bucket_name = ""
    lowest_window = ""

    summary_lines = []

    for grp in raw_groups:
        grp_name = str(grp.get("name", "Unknown Group"))[:64]
        grp_desc = str(grp.get("description", ""))[:256]
        raw_buckets = grp.get("buckets", [])[:10]

        # Assign friendly icon
        is_gemini = "gemini" in grp_name.lower()
        grp_icon = "󰘧" if is_gemini else "󰚩"

        parsed_buckets = []
        bucket_summaries = []

        for b in raw_buckets:
            b_id = str(b.get("id", ""))[:64]
            b_name = str(b.get("name", "Limit"))[:64]
            b_window = str(b.get("window", ""))[:32]
            b_desc = str(b.get("description", ""))[:256]
            rem_frac = float(b.get("remaining_fraction", 1.0))
            rem_pct = max(0, min(100, int(round(rem_frac * 100))))
            used_pct = 100 - rem_pct
            reset_time = str(b.get("reset_time", ""))[:64]
            countdown = format_countdown(reset_time)
            local_reset = format_local_time(reset_time)
            exact_reset = format_exact_time(reset_time)

            if rem_pct < lowest_pct:
                lowest_pct = rem_pct
                lowest_bucket_name = f"{grp_name} ({b_window})"
                lowest_window = b_window

            # Clean friendly window title
            window_title = "5-Hour Window" if b_window == "5h" else ("Weekly Window" if b_window == "weekly" else b_window.capitalize())

            bucket_obj = {
                "id": b_id,
                "name": b_name,
                "window": b_window,
                "window_title": window_title,
                "description": b_desc,
                "remaining_fraction": rem_frac,
                "remaining_pct": rem_pct,
                "used_pct": used_pct,
                "reset_time": reset_time,
                "reset_countdown": countdown,
                "reset_local": local_reset,
                "reset_exact": exact_reset,
                "alarming": rem_pct <= 15,
                "warning": 15 < rem_pct <= 30,
            }
            parsed_buckets.append(bucket_obj)
            bucket_summaries.append(f"{b_window}: {rem_pct}%")

        if bucket_summaries:
            short_name = "Gemini" if is_gemini else "Claude/GPT"
            summary_lines.append(f"{short_name}: {' · '.join(bucket_summaries)}")

        parsed_groups.append({
            "name": grp_name,
            "description": grp_desc,
            "icon": grp_icon,
            "is_gemini": is_gemini,
            "buckets": parsed_buckets,
        })

    # Active model information
    active_model = None
    if model_raw and "command" in model_raw:
        m_data = model_raw.get("command", {}).get("data", {})
        if m_data:
            active_model = {
                "id": str(m_data.get("id", ""))[:64],
                "label": str(m_data.get("label", ""))[:128],
                "effort": str(m_data.get("effort", ""))[:32],
            }

    tooltip = "Antigravity Quota\n" + ("\n".join(summary_lines) if summary_lines else "No limits reported")

    return {
        "status": "ok",
        "last_updated": datetime.now().strftime("%H:%M:%S"),
        "timestamp": int(datetime.now().timestamp()),
        "description": general_desc,
        "overall": {
            "lowest_remaining_pct": lowest_pct,
            "lowest_bucket_name": lowest_bucket_name,
            "lowest_window": lowest_window,
            "alarming": lowest_pct <= 15,
            "warning": 15 < lowest_pct <= 30,
        },
        "active_model": active_model,
        "groups": parsed_groups,
        "tooltip": tooltip,
        "latency": latency_raw,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch Antigravity usage limits")
    parser.add_argument("--cached", action="store_true", help="Return cache if recent (<300s)")
    parser.add_argument("--cached-only", action="store_true", help="Return cache immediately if exists")
    parser.add_argument("--force", action="store_true", help="Force fresh fetch from agy")
    parser.add_argument("--max-age", type=int, default=300, help="Max cache age in seconds (default 300)")
    parser.add_argument(
        "--agy-path",
        default=None,
        help="Explicit path to the agy binary (required; also settable via "
        f"the {AGY_PATH_ENV_VAR} env var). Never auto-discovered.",
    )
    parser.add_argument(
        "--enable-network-checks",
        action="store_true",
        help="Enable active network ping and TTFB checks in latency tracker",
    )
    args = parser.parse_args()

    cache_dir_fd = get_cache_dir_fd()

    def _read_cache() -> Optional[Dict[str, Any]]:
        return read_cache(cache_dir_fd) if cache_dir_fd is not None else None

    def _write_cache(data: Dict[str, Any]) -> None:
        if cache_dir_fd is not None:
            write_cache(cache_dir_fd, data)

    try:
        # If --cached-only or --cached requested, check cache
        if args.cached or args.cached_only:
            cached_data = _read_cache()
            if cached_data is not None:
                cached_ts = cached_data.get("timestamp", 0)
                age = datetime.now().timestamp() - cached_ts
                if args.cached_only or (args.cached and age < args.max_age and not args.force):
                    print(json.dumps(cached_data, indent=2))
                    return

        agy_fd = find_agy_binary(args.agy_path)
        if agy_fd is None:
            # Fallback to cache if available
            cached = _read_cache()
            if cached is not None:
                cached["stale"] = True
                cached["warning"] = "Antigravity CLI path not configured or invalid; displaying cached data"
                print(json.dumps(cached, indent=2))
                return

            err_resp = {
                "status": "error",
                "error": (
                    "Antigravity CLI ('agy') path is not configured. Set the full path to "
                    "your agy binary in the plugin's settings (agyPath), or the "
                    f"{AGY_PATH_ENV_VAR} environment variable."
                ),
                "groups": [],
                "overall": {"lowest_remaining_pct": 0, "alarming": False, "warning": False},
                "tooltip": "Antigravity CLI path not configured",
            }
            print(json.dumps(err_resp, indent=2))
            return

        try:
            # Run /usage and /model in parallel, with a non-blocking wall-clock timeout on latency
            executor = ThreadPoolExecutor(max_workers=3)
            try:
                fut_usage = executor.submit(run_agy_command, agy_fd, "/usage")
                fut_model = executor.submit(run_agy_command, agy_fd, "/model")
                fut_latency = (
                    executor.submit(
                        get_latency_report,
                        enable_network=args.enable_network_checks
                    )
                    if get_latency_report
                    else None
                )
                usage_res = fut_usage.result()
                model_res = fut_model.result()
                latency_res = None
                if fut_latency is not None:
                    try:
                        latency_res = fut_latency.result(timeout=3.0)
                    except Exception:
                        latency_res = None
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            if not usage_res or usage_res.get("status") != "SUCCESS":
                # Attempt to use stale cache
                cached = _read_cache()
                if cached is not None:
                    cached["stale"] = True
                    print(json.dumps(cached, indent=2))
                    return

                err_resp = {
                    "status": "error",
                    "error": "Failed to get usage limits from agy.",
                    "groups": [],
                    "overall": {"lowest_remaining_pct": 0, "alarming": False, "warning": False},
                    "tooltip": "Failed to get Antigravity usage",
                }
                print(json.dumps(err_resp, indent=2))
                return

            result = parse_usage_data(usage_res, model_res, latency_res)

            # Save to cache
            _write_cache(result)

            print(json.dumps(result, indent=2))

        except Exception:
            cached = _read_cache()
            if cached is not None:
                cached["stale"] = True
                print(json.dumps(cached, indent=2))
                return

            err_resp = {
                "status": "error",
                "error": "Failed to retrieve quota from Antigravity CLI.",
                "groups": [],
                "overall": {"lowest_remaining_pct": 0, "alarming": False, "warning": False},
                "tooltip": "Failed to get Antigravity usage",
            }
            print(json.dumps(err_resp, indent=2))
        finally:
            os.close(agy_fd)
    finally:
        if cache_dir_fd is not None:
            os.close(cache_dir_fd)


if __name__ == "__main__":
    main()
