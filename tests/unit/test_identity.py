from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from garuda_tunnel.identity import IdentityCheckResult, verify_token


def _spawn_sleeper(token: str) -> subprocess.Popen[bytes]:
    """Spawn a long-lived child carrying ``GARUDA_TUNNEL_TOKEN=token`` in its environ."""
    env = {**os.environ, "GARUDA_TUNNEL_TOKEN": token}
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=env,
    )
    # Give the child a brief moment to enter the sleep so /proc/<pid>/environ is populated.
    for _ in range(50):
        try:
            if os.path.exists(f"/proc/{proc.pid}/environ"):
                break
        except OSError:
            pass
        time.sleep(0.01)
    return proc


def test_match_against_child_process() -> None:
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("identity check only validated on Linux and macOS")
    proc = _spawn_sleeper("abc-123")
    try:
        assert verify_token(proc.pid, "abc-123") == IdentityCheckResult.match
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_mismatch_against_child_process() -> None:
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("identity check only validated on Linux and macOS")
    proc = _spawn_sleeper("abc-123")
    try:
        assert verify_token(proc.pid, "wrong") == IdentityCheckResult.mismatch
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_not_found_for_unused_pid() -> None:
    # PID 2**31 - 1 is essentially never allocated; the syscall should ENOENT.
    assert verify_token(2**31 - 1, "anything") == IdentityCheckResult.not_found
