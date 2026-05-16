from __future__ import annotations

import pytest

from garuda_tunnel.exceptions import (
    DaemonError,
    GarudaTunnelError,
    RequiredTunnelFailure,
    SchemaValidationError,
    TunnelStartupError,
    exit_code_for,
)


def test_all_errors_inherit_base() -> None:
    for cls in [
        SchemaValidationError,
        TunnelStartupError,
        RequiredTunnelFailure,
        DaemonError,
    ]:
        assert issubclass(cls, GarudaTunnelError)


@pytest.mark.parametrize(
    "exc, expected_code",
    [
        (SchemaValidationError("bad", {"field": "host"}), 1),
        (RequiredTunnelFailure("nope", {"failed": ["a"]}), 2),
        (DaemonError("fork failed", {"errno": 12}), 4),
    ],
)
def test_exit_code_for_known_errors(exc: GarudaTunnelError, expected_code: int) -> None:
    assert exit_code_for(exc) == expected_code


def test_to_error_output_does_not_leak_secrets() -> None:
    err = SchemaValidationError("bad", {"ssh_pkey": "-----BEGIN PRIVATE KEY-----..."})
    out = err.to_error_output()
    assert out["error"] == "SchemaValidationError"
    assert out["message"] == "bad"
    assert "ssh_pkey" not in out["details"]
