"""Typed exception hierarchy with stable exit-code mapping."""

from __future__ import annotations

from typing import Any

_SECRET_KEYS = frozenset({"ssh_pkey", "ssh_password", "ssh_pkey_passphrase"})


def _scrub(details: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of details with secret keys (ssh_pkey/etc) removed."""
    return {k: v for k, v in details.items() if k not in _SECRET_KEYS}


class GarudaTunnelError(Exception):
    """Base class for every error this tool reports as structured JSON."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        """Store message and a scrubbed copy of details for JSON output."""
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = _scrub(details or {})

    def to_error_output(self) -> dict[str, Any]:
        """Serialise the exception into the public ErrorOutput dict shape."""
        return {
            "error": type(self).__name__,
            "message": self.message,
            "details": self.details,
        }


class SchemaValidationError(GarudaTunnelError):
    """Input JSON failed pydantic validation; details carries errors()."""


class TunnelStartupError(GarudaTunnelError):
    """A single node failed to open its transport or local forward."""


class RequiredTunnelFailure(GarudaTunnelError):
    """At least one required node could not be started; the daemon aborts."""


class DaemonError(GarudaTunnelError):
    """Generic daemon-side failure surfaced via the IPC handshake."""


class KubeParseError(GarudaTunnelError):
    """A kubeconfig could not be parsed or lacked a usable current-context."""


_EXIT_CODES: dict[type[GarudaTunnelError], int] = {
    SchemaValidationError: 1,
    RequiredTunnelFailure: 2,
    KubeParseError: 2,
    DaemonError: 4,
}


def exit_code_for(exc: GarudaTunnelError) -> int:
    """Map a domain exception to its CLI exit code; default to 1."""
    return _EXIT_CODES.get(type(exc), 1)
