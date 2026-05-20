"""Worker process entry point. Invoked via ``python -m garuda_tunnel._worker``.

Reads ``InputSchema`` JSON from stdin, acquires its identity lockfile, runs
``TunnelManager.start_all_and_build_output``, writes the IPC message to
``--ipc-fd``, then blocks on signals.

This module is not part of the public CLI surface.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
from typing import Any

from pydantic import ValidationError

from garuda_tunnel.exceptions import DaemonError
from garuda_tunnel.identity import _state_dir
from garuda_tunnel.manager import TunnelManager
from garuda_tunnel.schemas import ErrorOutput, InputSchema, OutputSchema

_SCHEMA_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB is more than enough for any sane input


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="garuda_tunnel._worker", add_help=False)
    parser.add_argument("--ipc-fd", type=int, required=True)
    parser.add_argument("--token", required=True)
    return parser.parse_args(argv)


def _acquire_identity_lock(token: str) -> int:
    """Create + flock the per-token lockfile; return the open fd.

    The fd must stay open for the worker's lifetime. The kernel releases the
    flock automatically when the process exits, clean or not.
    """
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state / f"{token}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise DaemonError(
            "identity lock already held — token collision",
            {"token": "<redacted>"},
        ) from exc
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    os.fsync(fd)
    return fd


def _release_identity_lock(lock_fd: int, token: str) -> None:
    """Unlink the lockfile and close the fd. Best-effort; never raises."""
    try:
        (_state_dir() / f"{token}.lock").unlink()
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def _read_schema_from_stdin() -> InputSchema:
    """Read the schema JSON from stdin (parent has closed its write end)."""
    raw = sys.stdin.buffer.read()
    if len(raw) > _SCHEMA_MAX_BYTES:
        raise DaemonError("schema pipe exceeded size limit", {"limit": _SCHEMA_MAX_BYTES})
    return InputSchema.model_validate_json(raw.decode("utf-8"))


def _write_message(fd: int, message: dict[str, Any]) -> None:
    payload = (json.dumps(message) + "\n").encode("utf-8")
    while payload:
        written = os.write(fd, payload)
        if written <= 0:
            raise DaemonError("short write to IPC pipe", {"remaining": len(payload)})
        payload = payload[written:]


def _report_pre_run_failure(ipc_fd: int, exc: BaseException) -> None:
    """Best-effort: write a daemon_error frame so parent does not block on empty pipe."""
    err = (
        exc
        if isinstance(exc, DaemonError)
        else DaemonError("worker failed before reporting", {"type": type(exc).__name__})
    )
    try:
        _write_message(ipc_fd, {"kind": "daemon_error", "payload": err.to_error_output()})
    except OSError:
        pass


async def _run(args: argparse.Namespace, lock_fd: int) -> int:
    try:
        schema = _read_schema_from_stdin()
    except (DaemonError, ValidationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _report_pre_run_failure(args.ipc_fd, exc)
        try:
            os.close(args.ipc_fd)
        except OSError:
            pass
        _release_identity_lock(lock_fd, args.token)
        return 4

    manager = TunnelManager(schema)

    try:
        result = await manager.start_all_and_build_output(pid=os.getpid(), token=args.token)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Worker top-level guard: any uncaught failure here must reach the
        # parent as a `daemon_error` IPC frame, otherwise the parent blocks
        # forever on an empty pipe.
        await manager.stop_all()
        _report_pre_run_failure(args.ipc_fd, exc)
        try:
            os.close(args.ipc_fd)
        except OSError:
            pass
        _release_identity_lock(lock_fd, args.token)
        return 4

    if isinstance(result, ErrorOutput):
        await manager.stop_all()
        _write_message(
            args.ipc_fd,
            {"kind": "required_failure", "payload": result.model_dump(mode="json")},
        )
        os.close(args.ipc_fd)
        _release_identity_lock(lock_fd, args.token)
        return 2

    assert isinstance(result, OutputSchema)
    _write_message(
        args.ipc_fd,
        {"kind": "success", "payload": result.model_dump(mode="json")},
    )
    os.close(args.ipc_fd)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    try:
        await stop_event.wait()
    finally:
        await manager.stop_all()
        _release_identity_lock(lock_fd, args.token)
    return 0


def main(argv: list[str] | None = None) -> None:
    """Worker entry point: parse args, acquire identity lock, run asyncio loop, exit hard."""
    args = _parse_args(argv)
    try:
        lock_fd = _acquire_identity_lock(args.token)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Lock acquisition runs BEFORE asyncio.run; the worker top-level guard
        # inside _run cannot catch failures here. Report via IPC so the parent
        # does not block on an empty pipe.
        _report_pre_run_failure(args.ipc_fd, exc)
        os._exit(4)
    rc = asyncio.run(_run(args, lock_fd))
    os._exit(rc)


if __name__ == "__main__":  # pragma: no cover - exercised by integration test
    main()
