from __future__ import annotations

import os
import pathlib
import signal
import time

from garuda_tunnel.daemon import spawn_daemon
from garuda_tunnel.identity import TOKEN_ENV_VAR
from garuda_tunnel.schemas import InputSchema


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
    schema = _make_empty_schema()
    message = spawn_daemon(schema)
    payload = message["payload"]
    pid = int(payload["pid"])
    token = str(payload["token"])
    try:
        assert _process_alive(pid), "worker process should be alive after IPC handshake"
        assert pid != os.getpid()
        assert token  # opaque non-empty token
        # The worker process must carry the token in its /proc/<pid>/environ
        # snapshot, populated by execve. This is the contract that makes
        # `stop --pid --token` usable.
        environ_blob = pathlib.Path(f"/proc/{pid}/environ").read_bytes()
        assert f"{TOKEN_ENV_VAR}={token}".encode() in environ_blob
    finally:
        if _process_alive(pid):
            os.kill(pid, signal.SIGTERM)
            _wait_until_dead(pid)


def test_spawn_daemon_propagates_required_failure() -> None:
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "broken": {
                    "host": "127.0.0.1",
                    "port": 1,  # nothing listens here
                    "user": "nobody",
                    "ssh_password": "no",
                    "remote_ports": [6443],
                    "ssh_options": {"connect_timeout": 2},
                }
            }
        }
    )
    message = spawn_daemon(schema)
    assert message["kind"] == "required_failure"
    payload = message["payload"]
    assert payload["error"] == "RequiredTunnelFailure"
