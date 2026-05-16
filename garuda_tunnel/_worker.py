"""Worker process entry point. Invoked via ``python -m garuda_tunnel._worker``.

Reads ``InputSchema`` JSON from the file descriptor passed via ``--schema-fd``,
runs ``TunnelManager.start_all_and_build_output``, writes the IPC message to
``--ipc-fd``, then blocks on signals.

This module is not part of the public CLI surface.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from typing import Any

from garuda_tunnel.exceptions import DaemonError
from garuda_tunnel.identity import TOKEN_ENV_VAR
from garuda_tunnel.manager import TunnelManager
from garuda_tunnel.schemas import ErrorOutput, InputSchema, OutputSchema

_SCHEMA_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB is more than enough for any sane input


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="garuda_tunnel._worker", add_help=False)
    parser.add_argument("--ipc-fd", type=int, required=True)
    parser.add_argument("--schema-fd", type=int, required=True)
    return parser.parse_args(argv)


def _read_schema(schema_fd: int) -> InputSchema:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(schema_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
        if sum(map(len, chunks)) > _SCHEMA_MAX_BYTES:
            raise DaemonError("schema pipe exceeded size limit", {"limit": _SCHEMA_MAX_BYTES})
    os.close(schema_fd)
    return InputSchema.model_validate_json(b"".join(chunks).decode("utf-8"))


def _redirect_standard_fds(log_file: str | None) -> None:
    # Redirect OS-level FDs 0/1/2 directly (the canonical daemonization idiom);
    # works regardless of whether sys.std* has been wrapped by anything.
    target_path = log_file if log_file is not None else os.devnull
    target = open(target_path, "ab", buffering=0)  # noqa: SIM115
    devnull_in = open(os.devnull, "rb")  # noqa: SIM115
    os.dup2(devnull_in.fileno(), 0)
    os.dup2(target.fileno(), 1)
    os.dup2(target.fileno(), 2)


def _write_message(fd: int, message: dict[str, Any]) -> None:
    payload = (json.dumps(message) + "\n").encode("utf-8")
    while payload:
        written = os.write(fd, payload)
        if written <= 0:
            raise DaemonError("short write to IPC pipe", {"remaining": len(payload)})
        payload = payload[written:]


def _install_signal_handlers() -> None:
    def handler(_signum: int, _frame: object) -> None:
        os._exit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _wait_forever() -> None:
    event = threading.Event()
    event.wait()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    token = os.environ.get(TOKEN_ENV_VAR, "")
    manager = None
    try:
        schema = _read_schema(args.schema_fd)
        _redirect_standard_fds(schema.daemon.log_file)
        manager = TunnelManager(schema)
        result = manager.start_all_and_build_output(pid=os.getpid(), token=token)
        if isinstance(result, OutputSchema):
            _write_message(
                args.ipc_fd,
                {"kind": "success", "payload": result.model_dump(mode="json")},
            )
            os.close(args.ipc_fd)
        else:
            assert isinstance(result, ErrorOutput)
            manager.stop_all()
            _write_message(
                args.ipc_fd,
                {"kind": "required_failure", "payload": result.model_dump(mode="json")},
            )
            os.close(args.ipc_fd)
            os._exit(2)
    except Exception as exc:  # noqa: BLE001 - convert any failure into IPC daemon_error
        if manager is not None:
            manager.stop_all()
        err = (
            exc
            if isinstance(exc, DaemonError)
            else DaemonError("worker failed before reporting", {"type": type(exc).__name__})
        )
        try:
            _write_message(
                args.ipc_fd,
                {"kind": "daemon_error", "payload": err.to_error_output()},
            )
        except OSError:
            pass
        os._exit(4)

    _install_signal_handlers()
    _wait_forever()


if __name__ == "__main__":  # pragma: no cover - exercised by integration test
    main()
