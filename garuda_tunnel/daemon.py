"""Spawn the worker daemon via subprocess.Popen with a dedicated IPC pipe."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import subprocess
import sys
from typing import IO, Any

from garuda_tunnel.exceptions import DaemonError
from garuda_tunnel.identity import _state_dir
from garuda_tunnel.schemas import InputSchema


def _sweep_stale_lockfiles() -> None:
    """Remove lock files whose flock no one holds. Best-effort; ignores errors.

    Race-safe against concurrent daemons: a live daemon holds an exclusive
    flock, so LOCK_NB fails for it and we skip its file. Only files where
    no one holds the lock are unlinked.
    """
    state = _state_dir()
    try:
        candidates = list(state.glob("*.lock"))
    except OSError:
        return
    for path in candidates:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Held by a live daemon — leave it.
                continue
            # Got it — file is stale. Release and unlink.
            fcntl.flock(fd, fcntl.LOCK_UN)
            try:
                path.unlink()
            except OSError:
                pass
        finally:
            os.close(fd)


def _open_log_target(path: str | None) -> int | IO[bytes]:
    """Return a file or DEVNULL suitable for Popen's stdout/stderr arg.

    DEVNULL is the integer sentinel ``subprocess.DEVNULL``. A real path opens
    in append-binary mode so multiple daemons can share a log file. Caller
    must close the file in the parent after Popen returns; the worker keeps
    its own dup'd fd.
    """
    if path is None:
        return subprocess.DEVNULL
    return open(path, "ab", buffering=0)  # noqa: SIM115  # closed by caller


def spawn_daemon(schema: InputSchema) -> dict[str, Any]:
    """Spawn the worker, send the schema, read the IPC response, return it.

    Returns the structured IPC message for any of the three worker outcomes:
    ``success``, ``required_failure``, ``daemon_error``. Callers dispatch on
    ``message["kind"]`` and map to CLI exit codes.

    Raises ``DaemonError`` only when the parent itself cannot complete the
    handshake (empty pipe, malformed JSON, unknown kind).
    """
    _sweep_stale_lockfiles()
    runtime_token = secrets.token_urlsafe(32)

    ipc_read_fd, ipc_write_fd = os.pipe()
    log_target = _open_log_target(schema.daemon.log_file)
    try:
        proc = subprocess.Popen(  # noqa: SIM115  # pylint: disable=consider-using-with  # detached; never wait()ed
            [
                sys.executable,
                "-m",
                "garuda_tunnel._worker",
                f"--ipc-fd={ipc_write_fd}",
                f"--token={runtime_token}",
            ],
            stdin=subprocess.PIPE,
            stdout=log_target,
            stderr=log_target,
            pass_fds=[ipc_write_fd],
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(ipc_write_fd)
        if isinstance(log_target, int):
            # subprocess.DEVNULL is an int sentinel; nothing to close.
            pass
        else:
            log_target.close()

    assert proc.stdin is not None
    try:
        proc.stdin.write(schema.model_dump_json().encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        # Worker died before reading schema. Still try to read the IPC pipe;
        # the worker's main() guard may have written a daemon_error frame.
        proc.stdin = None
        _ = exc  # discarded; we surface via the IPC read path below

    return _read_ipc_response(ipc_read_fd)


def _read_ipc_response(read_fd: int) -> dict[str, Any]:
    """Block on the IPC pipe until EOF, parse, and return the message."""
    try:
        with os.fdopen(read_fd, "rb") as reader:
            raw = reader.read()
    except OSError as exc:
        raise DaemonError("failed to read worker IPC pipe", {"errno": exc.errno}) from exc

    if not raw:
        raise DaemonError("worker IPC pipe closed without a message", {})

    try:
        message: dict[str, Any] = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DaemonError("worker IPC produced invalid JSON", {"position": exc.pos}) from exc

    kind = message.get("kind")
    if kind in {"success", "required_failure", "daemon_error"}:
        return message
    raise DaemonError("unexpected IPC message kind", {"kind": str(kind)})
