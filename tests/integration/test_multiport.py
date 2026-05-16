from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def test_two_forwards_to_same_remote_port(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[tuple[int, str]],
) -> None:
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_ports": [6443, 6443],
            }
        }
    }
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0
    body = outcome["json"]
    entries = body["connections"]["a"]
    assert len(entries) == 2
    assert entries[0]["local_port"] != entries[1]["local_port"]
    started_daemons.append((body["pid"], body["token"]))
