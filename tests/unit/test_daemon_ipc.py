"""Daemon spawn IPC handshake.

Validates: spawn_daemon returns worker pid via IPC and surfaces
RequiredTunnelFailure when the worker reports unrecoverable startup
errors.
Code: tunstrap/daemon.py
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import time

import pytest

from tunstrap.daemon import spawn_daemon
from tunstrap.identity import IdentityCheckResult, verify_session
from tunstrap.schemas import InputSchema

pytestmark = pytest.mark.unit


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _make_empty_schema() -> InputSchema:
    # A schema with zero nodes: the worker returns success with empty
    # connections, which is exactly what the IPC handshake needs to test.
    return InputSchema.model_validate({"nodes": {}})


def test_spawn_daemon_returns_worker_pid_via_ipc() -> None:
    """Worker pid is delivered over the IPC pipe and the process holds the lock."""
    schema = _make_empty_schema()
    session_root = tempfile.mkdtemp(prefix="gt-test-")
    try:
        message = spawn_daemon(schema, session_dir=session_root)
        payload = message["payload"]
        pid = int(payload["pid"])
        try:
            assert _process_alive(pid), "worker process should be alive after IPC handshake"
            assert pid != os.getpid()
            # The worker process must hold <session_dir>/session.lock so that
            # `stop --session-dir` can verify identity. Exercise this via the
            # public identity API, which checks for an exclusive flock.
            if sys.platform not in {"linux", "darwin"}:
                pytest.skip("session identity check only validated on Linux and macOS")
            assert verify_session(payload["session_dir"], pid) == IdentityCheckResult.match
        finally:
            if _process_alive(pid):
                os.kill(pid, signal.SIGTERM)
                _wait_until_dead(pid)
    finally:
        shutil.rmtree(session_root, ignore_errors=True)


def test_spawn_daemon_propagates_required_failure() -> None:
    """A required node that cannot start surfaces as IPC required_failure."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "broken": {
                    "host": "127.0.0.1",
                    "port": 1,  # nothing listens here
                    "user": "nobody",
                    "ssh_password": "no",
                    "remote_targets": {"p": "127.0.0.1:6443"},
                    "ssh_options": {"connect_timeout": 2},
                }
            }
        }
    )
    session_root = tempfile.mkdtemp(prefix="gt-test-")
    try:
        message = spawn_daemon(schema, session_dir=session_root)
        assert message["kind"] == "required_failure"
        payload = message["payload"]
        assert payload["error"] == "RequiredTunnelFailure"
    finally:
        shutil.rmtree(session_root, ignore_errors=True)
