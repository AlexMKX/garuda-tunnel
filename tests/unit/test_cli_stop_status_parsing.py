from __future__ import annotations

from click.testing import CliRunner

from garuda_tunnel.cli import main


def test_stop_requires_pid_and_token() -> None:
    result = CliRunner().invoke(main, ["stop"])
    assert result.exit_code == 64

    result_no_token = CliRunner().invoke(main, ["stop", "--pid", "123"])
    assert result_no_token.exit_code == 64


def test_status_requires_pid() -> None:
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 64


def test_stop_bad_pid_int_exits_64() -> None:
    result = CliRunner().invoke(main, ["stop", "--pid", "not-an-int", "--token", "t"])
    assert result.exit_code == 64
