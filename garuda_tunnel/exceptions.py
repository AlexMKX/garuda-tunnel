"""Typed exception hierarchy with stable exit-code mapping."""

from __future__ import annotations

from typing import Any

_SECRET_KEYS = frozenset({"ssh_pkey", "ssh_password", "ssh_pkey_passphrase"})


def _scrub(details: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in details.items() if k not in _SECRET_KEYS}


class GarudaTunnelError(Exception):
    """Base class for every error this tool reports as structured JSON."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = _scrub(details or {})

    def to_error_output(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": self.message,
            "details": self.details,
        }


class SchemaValidationError(GarudaTunnelError):
    pass


class TunnelStartupError(GarudaTunnelError):
    pass


class RequiredTunnelFailure(GarudaTunnelError):
    pass


class DaemonError(GarudaTunnelError):
    pass


_EXIT_CODES: dict[type[GarudaTunnelError], int] = {
    SchemaValidationError: 1,
    RequiredTunnelFailure: 2,
    DaemonError: 4,
}


def exit_code_for(exc: GarudaTunnelError) -> int:
    """Map a domain exception to its CLI exit code; default to 1."""
    return _EXIT_CODES.get(type(exc), 1)
