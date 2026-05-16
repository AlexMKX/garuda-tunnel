"""Command-line interface. Subcommands are added in later tasks."""

from __future__ import annotations

import sys

import click

from garuda_tunnel import __version__


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


if __name__ == "__main__":  # pragma: no cover
    main()
