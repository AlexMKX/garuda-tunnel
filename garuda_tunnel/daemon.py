"""POSIX double-fork + execve daemon launcher with an IPC pipe."""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Any

from garuda_tunnel.exceptions import DaemonError
from garuda_tunnel.identity import TOKEN_ENV_VAR
from garuda_tunnel.schemas import InputSchema


def spawn_daemon(schema: InputSchema) -> dict[str, Any]:
    """Fork twice, execve into the worker, return the worker's IPC message.

    Returns the structured IPC message for any of the three worker outcomes:
    ``success``, ``required_failure``, ``daemon_error``. Callers dispatch on
    ``message["kind"]`` and map to CLI exit codes.

    Raises ``DaemonError`` only when the parent itself cannot complete the
    handshake (empty pipe, malformed JSON, unknown kind).
    """
    ipc_read_fd, ipc_write_fd = os.pipe()
    schema_read_fd, schema_write_fd = os.pipe()
    runtime_token = secrets.token_urlsafe(32)

    first_pid = os.fork()
    if first_pid > 0:
        # Parent process.
        os.close(ipc_write_fd)
        os.close(schema_read_fd)
        os.write(schema_write_fd, schema.model_dump_json().encode("utf-8"))
        os.close(schema_write_fd)
        return _parent_wait(ipc_read_fd, child_pid=first_pid)

    # First child.
    os.close(ipc_read_fd)
    os.close(schema_write_fd)
    try:
        os.setsid()
        os.umask(0)
        second_pid = os.fork()
        if second_pid > 0:
            os._exit(0)
        # Pre-daemon: execve into the worker. After this call the kernel
        # replaces the process image entirely; the new envp contains the
        # runtime token, which is what makes /proc/<pid>/environ usable for
        # stop/status identity checks.
        # Python sets O_CLOEXEC on pipe FDs by default (PEP 446); clear it
        # so the freshly-exec'd worker inherits the IPC and schema FDs.
        os.set_inheritable(ipc_write_fd, True)
        os.set_inheritable(schema_read_fd, True)
        env = {**os.environ, TOKEN_ENV_VAR: runtime_token}
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "garuda_tunnel._worker",
                "--ipc-fd",
                str(ipc_write_fd),
                "--schema-fd",
                str(schema_read_fd),
            ],
            env,
        )
    except BaseException as exc:  # noqa: BLE001 - second-fork child must never return
        # Any failure in the second-fork child (execve raise, MemoryError,
        # KeyboardInterrupt, etc.) must terminate this process; otherwise the
        # forked Python would unwind out of spawn_daemon back into whatever
        # callsite it inherited from the parent. Best-effort: tell the parent
        # via IPC so it does not block on a dead pipe forever.
        try:
            if isinstance(exc, OSError):
                err = DaemonError(
                    "failed to launch worker",
                    {"errno": exc.errno},
                )
            else:
                err = DaemonError(
                    "worker pre-launch raised",
                    {"type": type(exc).__name__},
                )
            os.write(
                ipc_write_fd,
                json.dumps(
                    {
                        "kind": "daemon_error",
                        "payload": err.to_error_output(),
                    }
                ).encode("utf-8"),
            )
        except OSError:
            pass
        os._exit(4)


def _parent_wait(read_fd: int, *, child_pid: int) -> dict[str, Any]:
    try:
        with os.fdopen(read_fd, "rb") as reader:
            raw = reader.read()
    except OSError as exc:
        raise DaemonError("failed to read worker IPC pipe", {"errno": exc.errno}) from exc

    # Reap the first-fork child; the worker is reparented to PID 1.
    try:
        os.waitpid(child_pid, 0)
    except ChildProcessError:
        pass

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
