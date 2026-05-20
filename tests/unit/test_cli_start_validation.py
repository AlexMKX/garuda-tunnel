"""CLI start input validation.

Validates: garuda_tunnel/cli.py start command rejects invalid stdin and
legacy fields with structured SchemaValidationError output.
Code: garuda_tunnel/cli.py
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from garuda_tunnel.cli import main

pytestmark = pytest.mark.unit


def test_start_rejects_invalid_json_with_exit_1() -> None:
    """Non-JSON stdin is reported as SchemaValidationError (exit 1)."""
    result = CliRunner().invoke(main, ["start"], input="not json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "SchemaValidationError"


def test_start_rejects_legacy_require_field() -> None:
    """The retired top-level `require` field is rejected by extra=forbid."""
    body = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            },
            "require": ["a"],
        }
    )
    result = CliRunner().invoke(main, ["start"], input=body)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "SchemaValidationError"
    assert "require" in json.dumps(payload["details"])
