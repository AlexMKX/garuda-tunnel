"""Multi-port local forwarding.

Validates: a node with two entries pointing at the same remote port
produces two distinct local listeners.
Code: garuda_tunnel/ssh.py::open_local_forwards
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def test_two_forwards_to_same_remote_port(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[tuple[int, str]],
) -> None:
    """Two forwards to the same remote port yield distinct local ports."""
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_targets": {
                    "p1": "127.0.0.1:6443",
                    "p2": "127.0.0.1:6443",
                },
            }
        }
    }
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0
    body = outcome["json"]
    node_out = body["connections"]["a"]
    entries = node_out["ports"]
    assert len(entries) == 2
    assert entries["p1"] != entries["p2"]
    assert node_out["fetch_files"] == {}
    started_daemons.append((body["pid"], body["token"]))
