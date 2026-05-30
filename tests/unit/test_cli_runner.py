"""CLI runner unit tests.

Validates: garuda_tunnel/cli.py command surface (start/stop/status)
including exit codes, JSON output, and error paths.
Code: garuda_tunnel/cli.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from garuda_tunnel import cli as cli_mod
from garuda_tunnel.cli import main

pytestmark = pytest.mark.unit


def _patch_spawn_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        return {
            "kind": "success",
            "payload": {
                "connections": {},
                "pid": 4242,
                "token": "tk",
                "started_at": "2026-05-20T00:00:00Z",
                "warnings": [],
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn_daemon)


def test_start_success_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy-path start returns 0 and prints success JSON."""
    _patch_spawn_success(monkeypatch)
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["pid"] == 4242
    assert out["token"] == "tk"


def test_start_required_failure_returns_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """RequiredTunnelFailure is surfaced via exit code 2."""

    def fake_spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        return {
            "kind": "required_failure",
            "payload": {
                "error": "RequiredTunnelFailure",
                "message": "required tunnel(s) failed to start",
                "details": {"failed": [{"node": "a", "error": "boom"}]},
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn_daemon)
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 2
    out = json.loads(result.output)
    assert out["error"] == "RequiredTunnelFailure"


def test_start_daemon_error_returns_four(monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon_error IPC kind surfaces via exit code 4."""

    def fake_spawn_daemon(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        return {
            "kind": "daemon_error",
            "payload": {
                "error": "DaemonError",
                "message": "worker failed",
                "details": {},
            },
        }

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn_daemon)
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 4
    out = json.loads(result.output)
    assert out["error"] == "DaemonError"


def test_status_with_session_dir_uses_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status --session-dir threads <sd>/tunnel-data as state_dir to verify_token."""
    from garuda_tunnel.identity import IdentityCheckResult

    captured: dict[str, Path | None] = {"state_dir": None}

    def fake_verify(_pid: int, _token: str, state_dir: Path | None = None) -> object:
        captured["state_dir"] = state_dir
        return IdentityCheckResult.match

    import garuda_tunnel.cli as cli_mod

    monkeypatch.setattr(cli_mod, "verify_token", fake_verify)
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.main,
        ["status", "--pid", str(os.getpid()), "--token", "tok", "--session-dir", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert captured["state_dir"] == (tmp_path.resolve() / "tunnel-data")


def test_stop_session_error_reports_and_exits_zero(tmp_path: Path) -> None:
    """stop --session-dir <nonexistent> returns structured JSON + exit 0."""
    from garuda_tunnel.cli import main as cli_main

    runner = CliRunner()
    missing = tmp_path / "no-such-session"
    result = runner.invoke(cli_main, ["stop", "--session-dir", str(missing)])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["stopped"] is False
    assert (
        "cannot read identity" in payload["reason"].lower()
        or "no such" in payload["reason"].lower()
    )


def test_stop_removes_tunnel_data_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop removes <session-dir>/tunnel-data after a successful match path."""
    import garuda_tunnel.cli as cli_mod
    from garuda_tunnel.identity import IdentityCheckResult

    sd = tmp_path / "session"
    data = sd / "tunnel-data"
    data.mkdir(parents=True)
    (data / "daemon.pid").write_text(f"{os.getpid()}\n")
    (data / "token").write_text("tok\n")

    def fake_verify(_pid: int, _token: str, state_dir: Path | None = None) -> object:
        return IdentityCheckResult.match

    call_count = {"n": 0}

    def fake_kill(_pid: int, sig: int) -> None:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise ProcessLookupError

    monkeypatch.setattr(cli_mod, "verify_token", fake_verify)
    monkeypatch.setattr(os, "kill", fake_kill)

    runner = CliRunner()
    result = runner.invoke(cli_mod.main, ["stop", "--session-dir", str(sd)])
    assert result.exit_code == 0
    assert not data.exists(), f"tunnel-data should be removed; result={result.output!r}"


def _make_session_dir(pid: int, token: str) -> str:
    """Create a temp session dir with daemon.pid and token files under tunnel-data/."""
    sd = tempfile.mkdtemp()
    data = Path(sd) / "tunnel-data"
    data.mkdir()
    (data / "daemon.pid").write_text(f"{pid}\n")
    (data / "token").write_text(f"{token}\n")
    return sd


def test_stop_unknown_pid_reports_not_found() -> None:
    """stop with a session dir pointing at a non-existent PID reports stopped=False."""
    sd = _make_session_dir(99999999, "x")
    result = CliRunner().invoke(main, ["stop", "--session-dir", sd])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out == {"stopped": False, "reason": "not found"}


def test_stop_token_mismatch_reports_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop with a session dir pointing at a mismatched token reports stopped=False."""
    from garuda_tunnel.identity import IdentityCheckResult

    monkeypatch.setattr(
        cli_mod,
        "verify_token",
        lambda pid, token, state_dir=None: IdentityCheckResult.mismatch,
    )
    sd = _make_session_dir(12345, "x")
    result = CliRunner().invoke(main, ["stop", "--session-dir", sd])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out == {"stopped": False, "reason": "token mismatch"}


def test_stop_identity_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop reports unavailable identity (e.g., /proc not readable)."""
    from garuda_tunnel.identity import IdentityCheckResult

    monkeypatch.setattr(
        cli_mod,
        "verify_token",
        lambda pid, token, state_dir=None: IdentityCheckResult.unavailable,
    )
    sd = _make_session_dir(12345, "x")
    result = CliRunner().invoke(main, ["stop", "--session-dir", sd])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out == {"stopped": False, "reason": "identity check unavailable"}


def test_status_unknown_pid_reports_not_alive() -> None:
    """status on a PID that does not exist returns alive=false."""
    result = CliRunner().invoke(main, ["status", "--pid", "99999999", "--token", "x"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out == {"alive": False}


def test_status_no_token_pid_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """status without token returns alive=true when the PID exists."""
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    result = CliRunner().invoke(main, ["status", "--pid", "12345"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out == {"alive": True}


def test_start_invalid_json_returns_one() -> None:
    """start with non-JSON stdin reports SchemaValidationError (exit 1)."""
    result = CliRunner().invoke(main, ["start"], input="not-json-at-all")
    assert result.exit_code == 1
    out = json.loads(result.output)
    assert out["error"] == "SchemaValidationError"


def test_start_schema_violation_returns_one() -> None:
    """start with a JSON object that fails InputSchema returns exit 1."""
    # Node without ssh_pkey/ssh_password triggers the cross-field validator.
    payload = json.dumps(
        {"nodes": {"a": {"host": "h", "user": "u", "remote_targets": {"p": "127.0.0.1:22"}}}}
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 1
    out = json.loads(result.output)
    assert out["error"] == "SchemaValidationError"


def test_start_unexpected_exception_returns_four(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unexpected exception in spawn_daemon is wrapped in DaemonError (exit 4)."""

    def boom(schema: Any, session_dir: str | None = None) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_mod, "spawn_daemon", boom)
    payload = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_targets": {"p": "127.0.0.1:22"},
                }
            }
        }
    )
    result = CliRunner().invoke(main, ["start"], input=payload)
    assert result.exit_code == 4
    out = json.loads(result.output)
    assert out["error"] == "DaemonError"
