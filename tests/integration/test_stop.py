"""garuda-tunnel stop against a real daemon.

Validates: stop terminates an alive daemon, refuses on wrong token,
and reports not-found on a non-existent PID.
Code: garuda_tunnel/cli.py::stop
"""

from __future__ import annotations

import json
import os
import subprocess
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
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(body["pid"]), "--token", body["token"]],
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
    """stop with a wrong token refuses and reports the reason.

    With flock-based identity, an unknown token has no lockfile on disk, so
    verify_token returns `not_found` (rather than the old `mismatch` produced
    by environ-parsing). The CLI reports this as ``reason: not found``; the
    daemon stays running and is cleaned up via the real token below.
    """
    body = _start(ssh_test_cluster)
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(body["pid"]), "--token", "bogus"],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert payload["reason"] in {"not found", "token mismatch"}
    started_daemons.append((body["pid"], body["token"]))


def test_stop_already_dead() -> None:
    """stop on a non-existent PID returns reason='not found'."""
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(2**31 - 1), "--token", "irrelevant"],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert payload["reason"] == "not found"
