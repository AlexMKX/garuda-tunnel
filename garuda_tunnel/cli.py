"""Command-line interface. Subcommands are added in later tasks."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import click
from pydantic import ValidationError

from garuda_tunnel import __version__
from garuda_tunnel.daemon import spawn_daemon
from garuda_tunnel.exceptions import (
    DaemonError,
    GarudaTunnelError,
    SchemaValidationError,
    exit_code_for,
)
from garuda_tunnel.identity import IdentityCheckResult, verify_token
from garuda_tunnel.schemas import InputSchema
from garuda_tunnel.session import SessionDir, SessionError


class _UsageExit64(click.Group):
    """Remap Click usage errors from default exit 2 to exit 64.

    Click's default ``standalone_mode=True`` swallows ``UsageError`` inside
    ``BaseCommand.main`` and exits with code 2 before any caller-level
    ``except`` block sees the exception. We force non-standalone mode so we
    can catch the error ourselves, render Click's usual message, and exit
    with the documented usage-error code (64, sysexits.h ``EX_USAGE``).
    """

    def main(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
        kwargs["standalone_mode"] = False
        try:
            return super().main(*args, **kwargs)  # type: ignore[call-overload]
        except click.UsageError as exc:
            exc.show()
            sys.exit(64)


@click.group(cls=_UsageExit64)
@click.version_option(__version__, prog_name="garuda-tunnel")
def main() -> None:
    """garuda-tunnel: SSH tunnel manager for ephemeral environments."""


@main.command("start")
@click.option("--session-dir", "session_dir", default=None)
def start_command(session_dir: str | None) -> None:
    """Read JSON from stdin, open tunnels, daemonize, print mapping JSON."""
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(
                "stdin is not valid JSON",
                {"position": exc.pos, "line": exc.lineno},
            ) from exc
        try:
            schema = InputSchema.model_validate(payload)
        except ValidationError as exc:
            raise SchemaValidationError(
                "input does not satisfy the InputSchema contract",
                {"errors": json.loads(exc.json())},
            ) from exc
        message = spawn_daemon(schema, session_dir=session_dir)
        sys.stdout.write(json.dumps(message["payload"]))
        sys.stdout.write("\n")
        sys.stdout.flush()
        kind = message["kind"]
        if kind == "required_failure":
            sys.exit(2)
        if kind == "daemon_error":
            sys.exit(4)
        # kind == "success" → exit 0 (default)
    except GarudaTunnelError as exc:
        sys.stdout.write(json.dumps(exc.to_error_output()))
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(exit_code_for(exc))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Top-level guard: surface any unexpected failure as DaemonError JSON and
        # exit 4 instead of dumping a Python traceback to a caller.
        sys.stdout.write(
            json.dumps(
                DaemonError(
                    "unexpected failure during start",
                    {"type": type(exc).__name__},
                ).to_error_output()
            )
        )
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(4)


@main.command("stop")
@click.option("--session-dir", "session_dir", required=True)
@click.option("--grace-seconds", type=int, default=10, show_default=True)
def stop_command(session_dir: str, grace_seconds: int) -> None:
    """Stop the daemon recorded under <session-dir>/tunnel-data and clean it up."""
    try:
        pid, token = SessionDir.read_identity(session_dir)
    except SessionError as exc:
        sys.stdout.write(json.dumps({"stopped": False, "reason": str(exc)}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(0)
        return
    data_dir = Path(session_dir).resolve() / "tunnel-data"
    _kill_with_identity(pid, token, grace_seconds, force=True, state_dir=data_dir)
    SessionDir.cleanup_path(session_dir)


@main.command("status")
@click.option("--pid", type=int, required=True)
@click.option("--token", type=str, default=None)
@click.option("--session-dir", "session_dir", default=None)
def status_command(pid: int, token: str | None, session_dir: str | None) -> None:
    """Report whether the daemon with the given PID is alive."""
    state_dir = (Path(session_dir).resolve() / "tunnel-data") if session_dir else None
    alive = _is_alive(pid, token, state_dir=state_dir)
    sys.stdout.write(json.dumps({"alive": alive}))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _is_alive(pid: int, token: str | None, state_dir: Path | None = None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        if token is None:
            return True
        # Fall through to verify_token; it owns the with-token answer.
    if token is None:
        return True
    return verify_token(pid, token, state_dir=state_dir) == IdentityCheckResult.match


def _kill_with_identity(  # pylint: disable=too-many-return-statements  # reason: each identity-check outcome maps to a distinct early return
    pid: int, token: str, grace_seconds: int, *, force: bool, state_dir: Path | None = None
) -> bool:
    check = verify_token(pid, token, state_dir=state_dir)
    if check == IdentityCheckResult.not_found:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "not found"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    if check == IdentityCheckResult.mismatch:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "token mismatch"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    if check == IdentityCheckResult.unavailable:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "identity check unavailable"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        sys.stdout.write(json.dumps({"stopped": True}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return True

    deadline = time.monotonic() + max(0, grace_seconds)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            sys.stdout.write(json.dumps({"stopped": True}))
            sys.stdout.write("\n")
            sys.stdout.flush()
            return True
        time.sleep(0.5)

    if not force:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "still alive"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False

    recheck = verify_token(pid, token, state_dir=state_dir)
    if recheck != IdentityCheckResult.match:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "identity changed during grace"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        sys.stdout.write(json.dumps({"stopped": True}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return True
    sys.stdout.write(json.dumps({"stopped": True, "forced": True}))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return True


if __name__ == "__main__":  # pragma: no cover
    main()
