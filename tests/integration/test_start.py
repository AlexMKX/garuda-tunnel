"""garuda-tunnel start integration scenarios.

Validates: start happy path across multiple nodes, required-node failure
cleanup, optional-node failures as warnings, and schema rejection.
Code: garuda_tunnel/cli.py, garuda_tunnel/manager.py
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def _node(host: str, port: int, pem: str, remote_port: int = 6443) -> dict[str, Any]:
    return {
        "host": host,
        "port": port,
        "user": "tester",
        "ssh_pkey": pem,
        "remote_targets": {"p": f"127.0.0.1:{remote_port}"},
    }


def test_start_all_required_success(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[tuple[int, str]],
) -> None:
    """Two-node all-required start returns both connections plus pid/token."""
    payload = {
        "nodes": {
            "a": _node(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-a"],
                ssh_test_cluster["private_pem"],
            ),
            "b": _node(
                "127.0.0.1",
                ssh_test_cluster["ports"]["sshd-b"],
                ssh_test_cluster["private_pem"],
            ),
        }
    }
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    assert sorted(body["connections"].keys()) == ["a", "b"]
    assert body["pid"] > 0
    assert body["token"]
    started_daemons.append((body["pid"], body["token"]))


def test_start_required_failure_cleans_up(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[tuple[int, str]],
) -> None:
    """Required-node auth failure aborts start with structured error."""
    good = _node(
        "127.0.0.1",
        ssh_test_cluster["ports"]["sshd-a"],
        ssh_test_cluster["private_pem"],
    )
    bad = {
        "host": "127.0.0.1",
        "port": ssh_test_cluster["ports"]["sshd-b"],
        "user": "tester",
        "ssh_pkey": "-----BEGIN OPENSSH PRIVATE KEY-----\nGARBAGE\n-----END OPENSSH PRIVATE KEY-----",
        "remote_targets": {"p": "127.0.0.1:6443"},
    }
    outcome = garuda_tunnel_start({"nodes": {"a": good, "b": bad}})
    # Pydantic accepts any string for ssh_pkey, so failure surfaces at
    # connect time as a required-tunnel failure (exit code 2), not as
    # schema validation (exit code 1).
    assert outcome["returncode"] == 2
    body = outcome["json"]
    assert body["error"] == "RequiredTunnelFailure"


def test_start_optional_failure_warns(
    ssh_test_cluster: dict[str, Any],
    started_daemons: list[tuple[int, str]],
) -> None:
    """Optional-node failure becomes a TunnelWarning, not an error."""
    good = _node(
        "127.0.0.1",
        ssh_test_cluster["ports"]["sshd-a"],
        ssh_test_cluster["private_pem"],
    )
    optional_bad = {
        "host": "127.0.0.1",
        "port": ssh_test_cluster["ports"]["sshd-c"],
        "user": "wrong-user",
        "ssh_pkey": ssh_test_cluster["private_pem"],
        "remote_targets": {"p": "127.0.0.1:6443"},
        "required": False,
    }
    payload = {"nodes": {"a": good, "b": optional_bad}}
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0
    body = outcome["json"]
    assert "a" in body["connections"]
    assert "b" not in body["connections"]
    assert any(w["node"] == "b" for w in body["warnings"])
    started_daemons.append((body["pid"], body["token"]))


def test_start_schema_failure_exits_1(ssh_test_cluster: dict[str, Any]) -> None:
    """A malformed input schema is reported as SchemaValidationError (exit 1)."""
    outcome = garuda_tunnel_start({"nodes": {"a": {"user": "tester"}}})
    assert outcome["returncode"] == 1
    body = outcome["json"]
    assert body["error"] == "SchemaValidationError"
