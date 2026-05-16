from __future__ import annotations

import json

from click.testing import CliRunner

from garuda_tunnel.cli import main


def test_start_rejects_invalid_json_with_exit_1() -> None:
    result = CliRunner().invoke(main, ["start"], input="not json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "SchemaValidationError"


def test_start_rejects_unknown_node_in_require() -> None:
    body = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_ports": [22],
                }
            },
            "require": ["missing"],
        }
    )
    result = CliRunner().invoke(main, ["start"], input=body)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "SchemaValidationError"
    assert "missing" in payload["message"] or "missing" in json.dumps(payload["details"])
