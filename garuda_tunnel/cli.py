"""Command-line interface. Subcommands are added in later tasks."""

from __future__ import annotations

import json
import sys

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
from garuda_tunnel.schemas import InputSchema


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
def start_command() -> None:
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
        message = spawn_daemon(schema)
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
    except Exception as exc:  # noqa: BLE001
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


if __name__ == "__main__":  # pragma: no cover
    main()
