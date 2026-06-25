"""CLI stop/status argument parsing.

Validates: required flags and type coercion on `tunstrap stop` and
`tunstrap status` subcommands.
Code: tunstrap/cli.py
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tunstrap.cli import main

pytestmark = pytest.mark.unit


def test_stop_requires_session_dir() -> None:
    """--session-dir is mandatory on stop; missing it yields exit 64."""
    result = CliRunner().invoke(main, ["stop"])
    assert result.exit_code == 64


def test_status_requires_session_dir() -> None:
    """--session-dir is mandatory on status; missing it yields exit 64."""
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 64
