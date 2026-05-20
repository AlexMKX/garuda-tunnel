"""InputSchema / NodeInput / OutputSchema base shape.

Validates: minimum-valid input, required pkey-or-password invariant,
and OutputSchema round-tripping.
Code: garuda_tunnel/schemas.py
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from garuda_tunnel.schemas import (
    DaemonOptions,
    InputSchema,
    NodeInput,
    NodeOutput,
    OutputSchema,
    SSHOptions,
    TunnelWarning,
)
from tests.unit.conftest import make_node

pytestmark = pytest.mark.unit


def test_valid_minimum_input_parses() -> None:
    """A minimum-valid InputSchema fills defaults for port/required/options."""
    schema = InputSchema.model_validate({"nodes": {"a": make_node()}})
    assert schema.nodes["a"].port == 22
    assert schema.nodes["a"].required is True
    assert isinstance(schema.nodes["a"], NodeInput)
    assert isinstance(schema.daemon, DaemonOptions)
    assert isinstance(schema.nodes["a"].ssh_options, SSHOptions)


def test_node_requires_pkey_or_password() -> None:
    """A node with neither ssh_pkey nor ssh_password is rejected."""
    payload = {"nodes": {"a": {"host": "h", "user": "u", "remote_targets": {"p": "127.0.0.1:22"}}}}
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate(payload)
    assert "ssh_pkey or ssh_password" in str(excinfo.value)


def test_missing_required_host_fails() -> None:
    """A node missing the required host field is rejected."""
    payload = {
        "nodes": {"a": {"user": "u", "ssh_password": "p", "remote_targets": {"p": "127.0.0.1:22"}}}
    }
    with pytest.raises(ValidationError):
        InputSchema.model_validate(payload)


def test_output_schema_round_trips() -> None:
    """OutputSchema round-trips losslessly through JSON."""
    out = OutputSchema(
        connections={
            "a": NodeOutput(
                ports={"p": 40001},
                fetch_files={},
            )
        },
        pid=12345,
        token="abc",
        started_at="2026-05-16T14:30:00Z",
        warnings=[TunnelWarning(node="b", error="auth failed")],
    )
    rebuilt = OutputSchema.model_validate_json(out.model_dump_json())
    assert rebuilt == out
