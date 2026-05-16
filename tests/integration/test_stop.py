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
                "remote_ports": [6443],
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
    body = _start(ssh_test_cluster)
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(body["pid"]), "--token", "bogus"],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert "token" in payload["reason"] or "identity" in payload["reason"]
    started_daemons.append((body["pid"], body["token"]))


def test_stop_already_dead() -> None:
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(2**31 - 1), "--token", "irrelevant"],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert payload["reason"] == "not found"
