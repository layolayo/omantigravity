#!/usr/bin/env python3
"""
Automated security test suite for Omantigravity (#6127).
Verifies binary verification, symlink/FIFO resistance, bounded tail reads,
SSRF rejection, telemetry redaction, and cache sanitization.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from secure_fs import open_verified_chain, run_sandboxed_binary_fd
from check_latency import (
    validate_host,
    find_latest_log_name,
    read_log_tail,
    parse_log_text,
    MAX_LOG_READ_BYTES,
    DEFAULT_HOST,
)
from fetch_usage import sanitize_cache_payload, find_agy_binary


def test_ssrf_and_host_validation() -> None:
    print("Testing SSRF & Hostname validation...")
    invalid_hosts = [
        "-W",
        "--help",
        "127.0.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "0.0.0.0",
        "169.254.169.254",
        "::1",
        "localhost",
        "service.local",
        "internal.service.internal",
        "user@cloudcode.googleapis.com",
        "daily-cloudcode-pa.googleapis.com.",
        "daily-cloudcode-pa.googleapis.com/path",
        "daily-cloudcode-pa.googleapis.com; rm -rf /",
        "2130706433",  # Integer IPv4
        "0177.0.0.1",  # Octal IPv4
    ]
    for h in invalid_hosts:
        assert not validate_host(h), f"Host {h!r} should have been rejected"

    assert validate_host(DEFAULT_HOST), f"Default host {DEFAULT_HOST!r} should be valid"
    print("  ✓ SSRF & Hostname validation passed.")


def test_system_binary_fd_verification() -> None:
    print("Testing system binary fd-chain verification & /proc/self/fd execution...")
    ping_fd = open_verified_chain(Path("/usr/bin/ping"), want_dir=False, require_root=True)
    assert ping_fd is not None, "Failed to open verified /usr/bin/ping"
    try:
        out = run_sandboxed_binary_fd(ping_fd, ["-c", "1", "-W", "1", "--", DEFAULT_HOST], timeout=2.0)
        assert out is not None and "PING" in out, f"Execution via fd failed: {out}"
    finally:
        os.close(ping_fd)

    # Verify that a user-owned file is rejected when require_root=True
    cache_base = Path.home() / ".cache"
    with tempfile.NamedTemporaryFile(dir=str(cache_base)) as f:
        fake_bin = Path(f.name)
        fake_bin.chmod(0o755)
        fake_fd = open_verified_chain(fake_bin, want_dir=False, require_root=True)
        assert fake_fd is None, "Non-root binary must be rejected when require_root=True"

    print("  ✓ System binary fd-chain and /proc/self/fd exec passed.")


def test_symlink_and_fifo_resistance() -> None:
    print("Testing symlink & FIFO resistance...")
    cache_base = Path.home() / ".cache"
    with tempfile.TemporaryDirectory(dir=str(cache_base)) as tmpdir:
        tmp = Path(tmpdir)
        # 1. Real log
        real_log = tmp / "cli-20260915_120000.log"
        real_log.write_text("2026-09-15 normal log line\n")

        # 2. Symlinked log
        symlink_log = tmp / "cli-20260915_120001.log"
        symlink_log.symlink_to(real_log)

        # 3. FIFO log
        fifo_log = tmp / "cli-20260915_120002.log"
        os.mkfifo(str(fifo_log))

        dir_fd = open_verified_chain(tmp, want_dir=True, require_root=False)
        assert dir_fd is not None, "Failed to open directory fd"
        try:
            # find_latest_log_name must ONLY consider regular files (ignores symlink and FIFO)
            latest = find_latest_log_name(dir_fd)
            assert latest == "cli-20260915_120000.log", f"Expected real log, got {latest}"

            # Direct read_log_tail on symlink must be rejected
            assert read_log_tail(dir_fd, "cli-20260915_120001.log") is None, "Symlink must be rejected"

            # Direct read_log_tail on FIFO must be rejected without hanging
            assert read_log_tail(dir_fd, "cli-20260915_120002.log") is None, "FIFO must be rejected"
        finally:
            os.close(dir_fd)

    print("  ✓ Symlink & FIFO resistance passed.")


def test_bounded_tail_read() -> None:
    print("Testing 1 MiB bounded tail reading and partial-line discard...")
    cache_base = Path.home() / ".cache"
    with tempfile.TemporaryDirectory(dir=str(cache_base)) as tmpdir:
        tmp = Path(tmpdir)
        big_log = tmp / "cli-big.log"
        with open(big_log, "w") as f:
            f.write("OLD_HEADER_LINE_TO_DISCARD\n")
            for i in range(35000):
                f.write(f"Line marker {i:06d}: payload padding content for test\n")
            f.write("FINAL_TAIL_MARKER\n")

        assert big_log.stat().st_size > MAX_LOG_READ_BYTES
        dir_fd = open_verified_chain(tmp, want_dir=True, require_root=False)
        assert dir_fd is not None
        try:
            content = read_log_tail(dir_fd, "cli-big.log")
            assert content is not None
            assert len(content.encode("utf-8")) <= MAX_LOG_READ_BYTES
            assert "FINAL_TAIL_MARKER" in content
            assert "OLD_HEADER_LINE_TO_DISCARD" not in content
            # Ensure first line in content is not partial
            first_line = content.splitlines()[0]
            assert first_line.startswith("Line marker "), f"Partial line found: {first_line}"
        finally:
            os.close(dir_fd)

    print("  ✓ Bounded tail reading passed.")


def test_telemetry_redaction_and_sanitization() -> None:
    print("Testing telemetry redaction and error sanitization...")
    raw_log_sample = """
I0915 13:00:00.000000 1234 http_helpers.go:10] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent ResponseID: secret_resp_123 Trace: secret_trace_456
I0915 13:00:10.000000 1234 http_helpers.go:10] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent ResponseID: secret_resp_789 Trace: secret_trace_012
E0915 13:00:15.000000 1234 run.go:42] Run: attempt 1 failed (rpc error: code = 503 desc = No capacity available for model on /usr/internal/path/foo.go:123), retrying...
"""
    turns, errors = parse_log_text(raw_log_sample)
    assert len(turns) == 1
    assert "response_id" not in turns[0], "response_id must be redacted"
    assert "trace_id" not in turns[0], "trace_id must be redacted"
    assert turns[0]["duration_sec"] == 10.0

    assert len(errors) == 1
    assert errors[0]["is_capacity_error"] is True
    assert errors[0]["message"] == "Google servers at capacity (503)"
    assert "/usr/internal/path" not in errors[0]["message"], "Internal paths leaked"
    print("  ✓ Telemetry redaction and error sanitization passed.")


def test_cache_resanitization_on_read() -> None:
    print("Testing cache re-sanitization on read...")
    dirty_cache = {
        "groups": [{"name": "A" * 200, "buckets": [{"name": "B" * 200}]} for _ in range(25)],
        "latency": {
            "recent_turns": [{"trace_id": "LEAKED", "response_id": "LEAKED", "duration_sec": 5.0}],
            "recent_errors": [{"message": "RAW /home/user/leak.go:99 error", "is_capacity_error": True}],
        },
    }
    clean = sanitize_cache_payload(dirty_cache)
    assert clean is not None
    assert len(clean["groups"]) == 10, "Groups ceiling failed"
    assert len(clean["groups"][0]["name"]) <= 64, "Group name length ceiling failed"
    assert "trace_id" not in clean["latency"]["recent_turns"][0], "Trace ID leaked through cache"
    assert "response_id" not in clean["latency"]["recent_turns"][0], "Response ID leaked through cache"
    print("  ✓ Cache re-sanitization passed.")


def test_agypath_injection_rejection() -> None:
    print("Testing agyPath unsafe input rejection...")
    assert find_agy_binary("$HOME/bin/agy") is None, "Environment variable expansion must be rejected"
    assert find_agy_binary("/usr/bin/agy\0evil") is None, "Null bytes must be rejected"
    assert find_agy_binary("/usr/bin/agy\nevil") is None, "Line breaks must be rejected"
    assert find_agy_binary("relative/path/agy") is None, "Relative paths must be rejected"
    print("  ✓ agyPath injection rejection passed.")


def main() -> None:
    print("=== RUNNING OMANTIGRAVITY SECURITY TEST SUITE ===\n")
    test_ssrf_and_host_validation()
    test_system_binary_fd_verification()
    test_symlink_and_fifo_resistance()
    test_bounded_tail_read()
    test_telemetry_redaction_and_sanitization()
    test_cache_resanitization_on_read()
    test_agypath_injection_rejection()
    print("\n=== ALL SECURITY TESTS PASSED SUCCESSFULLY! ===")


if __name__ == "__main__":
    main()
