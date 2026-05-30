"""garuda-tunnel status against a real daemon.

Validates: alive-then-dead status transitions, plus wrong-token detection
through the real CLI binary.
Code: garuda_tunnel/cli.py::status
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def test_status_alive_then_dead(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[tuple[int, str]],
) -> None:
    """status flips from alive to dead after stop; wrong token reports not alive."""
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
    body = outcome["json"]
    session_dir = body["session_dir"]

    alive = subprocess.run(
        [
            "garuda-tunnel",
            "status",
            "--pid",
            str(body["pid"]),
            "--token",
            body["token"],
            "--session-dir",
            session_dir,
        ],
        capture_output=True,
        text=True,
    )
    assert json.loads(alive.stdout)["alive"] is True

    # A wrong token with the correct session-dir: the lock file for "bad"
    # does not exist in tunnel-data/, so verify_token returns not_found → alive=False.
    wrong = subprocess.run(
        [
            "garuda-tunnel",
            "status",
            "--pid",
            str(body["pid"]),
            "--token",
            "bad",
            "--session-dir",
            session_dir,
        ],
        capture_output=True,
        text=True,
    )
    assert json.loads(wrong.stdout)["alive"] is False

    subprocess.run(
        ["garuda-tunnel", "stop", "--session-dir", session_dir],
        capture_output=True,
    )

    dead = subprocess.run(
        [
            "garuda-tunnel",
            "status",
            "--pid",
            str(body["pid"]),
            "--token",
            body["token"],
            "--session-dir",
            session_dir,
        ],
        capture_output=True,
        text=True,
    )
    assert json.loads(dead.stdout)["alive"] is False
