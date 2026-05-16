from __future__ import annotations

import pytest
from pydantic import ValidationError

from garuda_tunnel.schemas import (
    ConnectionEntry,
    DaemonOptions,
    InputSchema,
    NodeInput,
    OutputSchema,
    SSHOptions,
    TunnelWarning,
)


def _node(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "host": "node1.example.net",
        "user": "ubuntu",
        "ssh_password": "p",
        "remote_ports": [6443],
    }
    base.update(overrides)
    return base


def test_valid_minimum_input_parses() -> None:
    schema = InputSchema.model_validate({"nodes": {"a": _node()}})
    assert schema.require == "*"
    assert schema.nodes["a"].port == 22
    assert isinstance(schema.nodes["a"], NodeInput)
    assert isinstance(schema.daemon, DaemonOptions)
    assert isinstance(schema.nodes["a"].ssh_options, SSHOptions)


def test_require_star_or_list() -> None:
    InputSchema.model_validate({"nodes": {"a": _node()}, "require": "*"})
    InputSchema.model_validate({"nodes": {"a": _node(), "b": _node()}, "require": ["a"]})


def test_require_references_unknown_node() -> None:
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate({"nodes": {"a": _node()}, "require": ["nope"]})
    assert "unknown nodes" in str(excinfo.value)


def test_node_requires_pkey_or_password() -> None:
    payload = {"nodes": {"a": {"host": "h", "user": "u", "remote_ports": [22]}}}
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate(payload)
    assert "ssh_pkey or ssh_password" in str(excinfo.value)


def test_missing_required_host_fails() -> None:
    payload = {"nodes": {"a": {"user": "u", "ssh_password": "p", "remote_ports": [22]}}}
    with pytest.raises(ValidationError):
        InputSchema.model_validate(payload)


def test_output_schema_round_trips() -> None:
    out = OutputSchema(
        connections={
            "a": [
                ConnectionEntry(
                    remote_host="127.0.0.1",
                    remote_port=6443,
                    local_host="127.0.0.1",
                    local_port=40001,
                )
            ]
        },
        pid=12345,
        token="abc",
        started_at="2026-05-16T14:30:00Z",
        warnings=[TunnelWarning(node="b", error="auth failed")],
    )
    rebuilt = OutputSchema.model_validate_json(out.model_dump_json())
    assert rebuilt == out
