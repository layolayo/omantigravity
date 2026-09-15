#!/usr/bin/env python3
"""
Secure filesystem and process execution utilities for Omantigravity.
Hardened against symlink races, binary substitution TOCTOU, and unkillable hangs.
Part of the Omantigravity Omarchy plugin suite (#6127).
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import stat
import subprocess
from typing import List, Optional


def owner_and_mode_ok(
    st: os.stat_result, *, writable_check: int, require_root: bool = False
) -> bool:
    """Check that file descriptor owner is root (or user if not require_root) and not group/other-writable."""
    allowed_uids = (0,) if require_root else (0, os.getuid())
    return st.st_uid in allowed_uids and not (st.st_mode & writable_check)


def open_verified_chain(
    resolved: Path, want_dir: bool, require_root: bool = False
) -> Optional[int]:
    """Open `resolved` by walking component by component with `openat(..., O_NOFOLLOW | O_CLOEXEC)`.

    Verifies on each open file descriptor that every ancestor directory and the target
    are owned by root (or user if require_root=False) and are not group/other-writable.
    Returns the open file descriptor or None. Callers must close the fd.
    """
    if not resolved.is_absolute():
        return None

    parts = resolved.parts[1:]
    if not parts:
        return None

    fd = os.open("/", os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            flags = os.O_NOFOLLOW | os.O_CLOEXEC
            flags |= os.O_DIRECTORY if not is_last or want_dir else os.O_RDONLY
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError:
                return None
            os.close(fd)
            fd = next_fd

            st = os.fstat(fd)
            if is_last:
                expect_type = stat.S_ISDIR if want_dir else stat.S_ISREG
                if not expect_type(st.st_mode):
                    return None
                if not owner_and_mode_ok(
                    st, writable_check=stat.S_IWGRP | stat.S_IWOTH, require_root=require_root
                ):
                    return None
                if not want_dir and not (st.st_mode & stat.S_IXUSR):
                    return None
            else:
                if not stat.S_ISDIR(st.st_mode):
                    return None
                if not owner_and_mode_ok(
                    st, writable_check=stat.S_IWGRP | stat.S_IWOTH, require_root=require_root
                ):
                    return None
        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def run_sandboxed_binary_fd(
    bin_fd: int, args: List[str], timeout: float = 2.0
) -> Optional[str]:
    """Execute the already-open, verified binary fd via /proc/self/fd symlink with closed env."""
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [f"/proc/self/fd/{bin_fd}"] + args,
            executable=f"/proc/self/fd/{bin_fd}",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"LC_ALL": "C"},
            start_new_session=True,
            pass_fds=(bin_fd,),
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
                proc.wait(timeout=0.5)
            except Exception:
                pass
    return None
