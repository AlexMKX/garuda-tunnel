from __future__ import annotations

import os
import signal
import time

import pytest

from garuda_tunnel.daemon import spawn_daemon_with_callback


def _fake_success_payload(token: str, pid: int) -> dict[str, object]:
    return {
        "kind": "success",
        "payload": {
            "connections": {},
            "pid": pid,
            "token": token,
            "started_at": "2026-05-16T14:30:00Z",
            "warnings": [],
        },
    }


def test_spawn_daemon_returns_daemon_pid_via_ipc() -> None:
    def startup(token: str) -> dict[str, object]:
        return _fake_success_payload(token, os.getpid())

    message = spawn_daemon_with_callback(startup_callback=startup, log_file=None)
    payload = message["payload"]
    pid = int(payload["pid"])
    token = str(payload["token"])
    try:
        # Daemon must outlive the parent's IPC read.
        for _ in range(20):
            if _process_alive(pid):
                break
            time.sleep(0.05)
        assert _process_alive(pid), "daemon process should be alive after IPC handshake"
        assert pid != os.getpid()
        assert token  # opaque non-empty token
    finally:
        if _process_alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(40):
                if not _process_alive(pid):
                    break
                time.sleep(0.05)


def test_spawn_daemon_propagates_required_failure() -> None:
    def startup(token: str) -> dict[str, object]:
        return {
            "kind": "required_failure",
            "payload": {
                "error": "RequiredTunnelFailure",
                "message": "boom",
                "details": {"failed": ["a"]},
            },
        }

    with pytest.raises(SystemExit) as excinfo:
        spawn_daemon_with_callback(startup_callback=startup, log_file=None)
    # The CLI translates this into exit 2; spawn_daemon_with_callback uses the
    # same SystemExit code so we can assert it directly.
    assert excinfo.value.code == 2


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
