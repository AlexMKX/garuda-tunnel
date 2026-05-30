"""garuda-tunnel stop against a real daemon.

Validates: stop terminates an alive daemon, refuses on wrong identity,
and reports not-found on a non-existent PID.
Code: garuda_tunnel/cli.py::stop
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def _start(ssh_test_cluster: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {"p": "127.0.0.1:6443"},
            }
        }
    }
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0
    assert outcome["json"] is not None
    return outcome["json"]


def test_stop_alive_daemon(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[tuple[int, str]],
) -> None:
    """stop on a live daemon kills the process and reports stopped=True."""
    body = _start(ssh_test_cluster)
    started_daemons.append((body["pid"], body["token"]))
    session_dir = body["session_dir"]
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--session-dir", session_dir],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    assert json.loads(stop.stdout)["stopped"] is True
    # The daemon process must no longer exist.
    try:
        os.kill(body["pid"], 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert alive is False


def test_stop_wrong_token(
    ssh_test_cluster: dict[str, Any], started_daemons: list[tuple[int, str]]
) -> None:
    """stop with a wrong identity refuses and reports the reason.

    We craft a session dir whose tunnel-data/ records the real PID but a
    bogus token.  SessionDir.read_identity reads pid + "bogus"; verify_token
    then sees the process is alive but the lock file for "bogus" does not
    exist, so it returns not_found. The CLI reports reason "not found";
    the daemon keeps running and is cleaned up via its real session dir.
    """
    body = _start(ssh_test_cluster)
    # Build a fake session dir: real PID, wrong token.
    with tempfile.TemporaryDirectory(prefix="garuda-stop-test-") as fake_session:
        data_dir = Path(fake_session) / "tunnel-data"
        data_dir.mkdir(mode=0o700)
        (data_dir / "daemon.pid").write_text(f"{body['pid']}\n")
        (data_dir / "token").write_text("bogus\n")

        stop = subprocess.run(
            ["garuda-tunnel", "stop", "--session-dir", fake_session],
            capture_output=True,
            text=True,
        )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert payload["reason"] in {"not found", "token mismatch"}
    # Clean up the real daemon.
    started_daemons.append((body["pid"], body["token"]))
    subprocess.run(
        ["garuda-tunnel", "stop", "--session-dir", body["session_dir"]],
        capture_output=True,
    )


def test_stop_already_dead() -> None:
    """stop on a non-existent PID returns reason='not found'."""
    with tempfile.TemporaryDirectory(prefix="garuda-stop-test-") as fake_session:
        data_dir = Path(fake_session) / "tunnel-data"
        data_dir.mkdir(mode=0o700)
        (data_dir / "daemon.pid").write_text(f"{2**31 - 1}\n")
        (data_dir / "token").write_text("irrelevant\n")

        stop = subprocess.run(
            ["garuda-tunnel", "stop", "--session-dir", fake_session],
            capture_output=True,
            text=True,
        )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert payload["reason"] == "not found"
