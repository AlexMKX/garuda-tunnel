"""Daemon spawn IPC handshake.

Validates: spawn_daemon returns worker pid/token via IPC and surfaces
RequiredTunnelFailure when the worker reports unrecoverable startup
errors.
Code: garuda_tunnel/daemon.py
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import pytest

from garuda_tunnel.daemon import spawn_daemon
from garuda_tunnel.identity import IdentityCheckResult, verify_token
from garuda_tunnel.schemas import InputSchema

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


def test_spawn_daemon_returns_worker_pid_and_token_via_ipc() -> None:
    """Worker pid and token are delivered over the IPC pipe and process is alive."""
    schema = _make_empty_schema()
    session_root = tempfile.mkdtemp(prefix="gt-test-")
    try:
        message = spawn_daemon(schema, session_dir=session_root)
        payload = message["payload"]
        pid = int(payload["pid"])
        token = str(payload["token"])
        try:
            assert _process_alive(pid), "worker process should be alive after IPC handshake"
            assert pid != os.getpid()
            assert token  # opaque non-empty token
            # The worker process must hold the identity lockfile so that
            # `stop --pid --token` can verify identity. Exercise this via the
            # public identity API, which checks for an exclusive flock on
            # <session_dir>/tunnel-data/<token>.lock.
            if sys.platform not in {"linux", "darwin"}:
                pytest.skip("token identity check only validated on Linux and macOS")
            data_dir = Path(payload["session_dir"]) / "tunnel-data"
            assert verify_token(pid, token, state_dir=data_dir) == IdentityCheckResult.match
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


def test_spawn_daemon_with_leading_dash_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `secrets.token_urlsafe` may return a leading-'-' string.

    A bare ``--token <value>`` argv with ``value`` starting with ``-`` would
    make argparse treat it as a separate flag (e.g. ``-AbC...``) and reject
    the parse. The fix is to use ``--token=<value>`` form in argv, which
    argparse never splits.
    """
    monkeypatch.setattr(
        "garuda_tunnel.daemon.secrets.token_urlsafe",
        lambda _nbytes: "-leading-dash-token-AbCdEf",
    )
    schema = _make_empty_schema()
    session_root = tempfile.mkdtemp(prefix="gt-test-")
    try:
        message = spawn_daemon(schema, session_dir=session_root)
        assert message["kind"] == "success"
        payload = message["payload"]
        pid = int(payload["pid"])
        try:
            assert _process_alive(pid)
            assert payload["token"] == "-leading-dash-token-AbCdEf"
        finally:
            if _process_alive(pid):
                os.kill(pid, signal.SIGTERM)
                _wait_until_dead(pid)
    finally:
        shutil.rmtree(session_root, ignore_errors=True)
