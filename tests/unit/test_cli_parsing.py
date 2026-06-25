"""CLI top-level parsing.

Validates: help, version, and unknown-subcommand behaviour of the
tunstrap CLI dispatcher.
Code: tunstrap/cli.py
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tunstrap.cli import main

pytestmark = pytest.mark.unit


def test_help_exits_zero() -> None:
    """Print top-level help and exit 0."""
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "tunstrap" in result.output


def test_version_flag() -> None:
    """Print package version and exit 0."""
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "tunstrap" in result.output


def test_unknown_subcommand_exits_64() -> None:
    """Reject an unknown subcommand with exit code 64 (usage error)."""
    result = CliRunner().invoke(main, ["does-not-exist"])
    assert result.exit_code == 64
