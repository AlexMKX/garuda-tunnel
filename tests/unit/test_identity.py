"""Process-identity verification via fcntl flock on a per-token file.

Validates: verify_token correctly identifies a child process by token via
an exclusive flock on ~/.local/state/garuda-tunnel/<token>.lock.
Code: garuda_tunnel/identity.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from garuda_tunnel.identity import IdentityCheckResult, _state_dir, verify_token

pytestmark = pytest.mark.unit


def _spawn_locker(token: str, recorded_pid: int | None = None) -> subprocess.Popen[bytes]:
    """Spawn a child that opens, flocks, and writes its PID to the token lockfile.

    The child holds the lock until killed. `recorded_pid` overrides what the
    child writes into the file (defaults to its own PID); used by the mismatch
    test to simulate a stale-PID record.
    """
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state / f"{token}.lock"
    code = textwrap.dedent(
        f"""
        import fcntl, os, signal, sys, time
        fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recorded = {recorded_pid!r}
        pid_to_write = recorded if recorded is not None else os.getpid()
        os.write(fd, f"{{pid_to_write}}\\n".encode("ascii"))
        os.fsync(fd)
        # Signal readiness via stdout so the test can wait deterministically.
        sys.stdout.write("ready\\n")
        sys.stdout.flush()
        signal.pause()
        """
    ).strip()
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
    )
    # Deterministic wait for the child to finish locking + writing.
    assert proc.stdout is not None
    ready = proc.stdout.readline()
    if not ready.startswith(b"ready"):
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"locker child never reported ready: {ready!r}")
    return proc


def _cleanup(proc: subprocess.Popen[bytes], token: str) -> None:
    """Kill the locker child and remove its lockfile."""
    proc.kill()
    proc.wait(timeout=5)
    try:
        (_state_dir() / f"{token}.lock").unlink()
    except FileNotFoundError:
        pass


def test_match_against_locked_child() -> None:
    """verify_token returns match when the child holds the lock and PID agrees."""
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("flock identity check requires POSIX flock semantics")
    token = "test-token-match-abc"
    proc = _spawn_locker(token)
    try:
        assert verify_token(proc.pid, token) == IdentityCheckResult.match
    finally:
        _cleanup(proc, token)


def test_mismatch_when_recorded_pid_differs() -> None:
    """verify_token returns mismatch when the file records a different PID.

    Simulates PID reuse: the file claims PID 99999 but the caller passes a
    different (real) PID. Daemon (locker) is alive, so the file is locked.
    """
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("flock identity check requires POSIX flock semantics")
    token = "test-token-mismatch-def"
    proc = _spawn_locker(token, recorded_pid=99999)
    try:
        # Caller passes proc.pid; file says 99999 → mismatch.
        assert verify_token(proc.pid, token) == IdentityCheckResult.mismatch
    finally:
        _cleanup(proc, token)


def test_not_found_for_unused_pid() -> None:
    """verify_token returns not_found for a PID that does not exist."""
    assert verify_token(2**31 - 1, "anything") == IdentityCheckResult.not_found


def test_not_found_when_lock_file_missing() -> None:
    """No lockfile on disk means the daemon never started for that token."""
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("flock identity check requires POSIX flock semantics")
    # Use a real living PID (this test process) with a token that has no file.
    token = "test-token-no-such-file-xyz"
    assert verify_token(os.getpid(), token) == IdentityCheckResult.not_found


def test_not_found_when_lock_file_stale() -> None:
    """A lockfile with no holder means the previous daemon died: treat as not_found."""
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("flock identity check requires POSIX flock semantics")
    token = "test-token-stale-ghi"
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state / f"{token}.lock"
    lock_path.write_bytes(b"12345\n")
    try:
        # File exists, no flock holder. verify_token must see it as stale.
        assert verify_token(12345, token) == IdentityCheckResult.not_found
    finally:
        lock_path.unlink(missing_ok=True)
