"""CLI stop/status argument parsing.

Validates: required flags and type coercion on `garuda-tunnel stop` and
`garuda-tunnel status` subcommands.
Code: garuda_tunnel/cli.py
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from garuda_tunnel.cli import main

pytestmark = pytest.mark.unit


def test_stop_requires_pid_and_token() -> None:
    """Both --pid and --token are mandatory; missing them yields exit 64."""
    result = CliRunner().invoke(main, ["stop"])
    assert result.exit_code == 64

    result_no_token = CliRunner().invoke(main, ["stop", "--pid", "123"])
    assert result_no_token.exit_code == 64


def test_status_requires_pid() -> None:
    """--pid is mandatory on status; missing it yields exit 64."""
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 64


def test_stop_bad_pid_int_exits_64() -> None:
    """Non-integer --pid is a usage error (exit 64)."""
    result = CliRunner().invoke(main, ["stop", "--pid", "not-an-int", "--token", "t"])
    assert result.exit_code == 64
