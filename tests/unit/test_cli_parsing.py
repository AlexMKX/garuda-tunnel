from __future__ import annotations

from click.testing import CliRunner

from garuda_tunnel.cli import main


def test_help_exits_zero() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "garuda-tunnel" in result.output


def test_version_flag() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "garuda-tunnel" in result.output


def test_unknown_subcommand_exits_64() -> None:
    result = CliRunner().invoke(main, ["does-not-exist"])
    assert result.exit_code == 64
