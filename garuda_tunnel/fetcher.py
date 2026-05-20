"""SFTP file fetch over a live asyncssh SSHClientConnection.

The fetcher rides the same SSH session as the local port forwards (no
second TCP connection, no second authentication). The SFTP channel is
multiplexed over the existing transport.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Final

import asyncssh

from garuda_tunnel.schemas import FetchedFile, FileSpec

# Exceptions we expect from a live SSH/SFTP session. Anything outside this
# tuple is a programmer error and must bubble up — never silently turned
# into a FetchedFile.error.
_SFTP_TRANSPORT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    asyncssh.Error,
    OSError,
    asyncio.TimeoutError,
)

_MAX_FETCH_BYTES: Final[int] = 1 << 20  # 1 MiB hard cap

_SFTP_ERRNO_NAMES: Final[dict[int, str]] = {
    1: "SSH_FX_EOF",
    2: "SSH_FX_NO_SUCH_FILE",
    3: "SSH_FX_PERMISSION_DENIED",
    4: "SSH_FX_FAILURE",
    5: "SSH_FX_BAD_MESSAGE",
    6: "SSH_FX_NO_CONNECTION",
    7: "SSH_FX_CONNECTION_LOST",
    8: "SSH_FX_OP_UNSUPPORTED",
}


class _CapExceeded(Exception):
    """Sentinel for the 1 MiB safety cap."""


def _classify_error(exc: BaseException) -> str:
    """Map an exception to the canonical FetchedFile.error string."""
    if isinstance(exc, asyncssh.SFTPError):
        return _SFTP_ERRNO_NAMES.get(int(exc.code), "SSH_FX_UNKNOWN")
    return type(exc).__name__


async def fetch_files(  # pylint: disable=too-many-branches  # reason: per-file error attribution branches
    conn: asyncssh.SSHClientConnection,
    specs: dict[str, FileSpec],
) -> tuple[dict[str, FetchedFile], list[str]]:
    """Fetch all files for a node over a single SFTP channel."""
    if not specs:
        return {}, []

    results: dict[str, FetchedFile] = {}
    required_failures: list[str] = []

    try:
        sftp_cm = conn.start_sftp_client()
    except _SFTP_TRANSPORT_ERRORS as exc:
        code = _classify_error(exc)
        for name, spec in specs.items():
            results[name] = FetchedFile(error=code)
            if spec.required:
                required_failures.append(name)
        return results, required_failures

    try:
        async with sftp_cm as sftp:
            for name, spec in specs.items():
                try:
                    stat = await sftp.stat(spec.path)
                    if stat.size is not None and stat.size > _MAX_FETCH_BYTES:
                        raise _CapExceeded
                    async with sftp.open(spec.path, "rb") as fh:
                        data = await fh.read(_MAX_FETCH_BYTES + 1)
                    raw: bytes = data if isinstance(data, bytes) else data.encode()
                    if len(raw) > _MAX_FETCH_BYTES:
                        raise _CapExceeded
                    results[name] = FetchedFile(
                        content_b64=base64.b64encode(raw).decode("ascii"),
                        size=len(raw),
                        sha256=hashlib.sha256(raw).hexdigest(),
                    )
                except _CapExceeded:
                    results[name] = FetchedFile(error="EFBIG")
                    if spec.required:
                        required_failures.append(name)
                except _SFTP_TRANSPORT_ERRORS as exc:
                    results[name] = FetchedFile(error=_classify_error(exc))
                    if spec.required:
                        required_failures.append(name)
    except _SFTP_TRANSPORT_ERRORS as exc:
        code = _classify_error(exc)
        for name, spec in specs.items():
            if name not in results:
                results[name] = FetchedFile(error=code)
                if spec.required:
                    required_failures.append(name)

    return results, required_failures
