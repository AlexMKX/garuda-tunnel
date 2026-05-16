"""POSIX double-fork daemonization with an IPC pipe used by the parent."""

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import threading
from typing import Any, Callable

from garuda_tunnel.exceptions import DaemonError
from garuda_tunnel.identity import TOKEN_ENV_VAR
from garuda_tunnel.manager import TunnelManager
from garuda_tunnel.schemas import ErrorOutput, InputSchema, OutputSchema

StartupCallback = Callable[[str], dict[str, Any]]
"""Returns a dict ``{"kind": "success" | "required_failure", "payload": {...}}``.
The ``payload`` is what the parent prints to stdout (after JSON-encoding)."""


def spawn_daemon(schema: InputSchema) -> dict[str, Any]:
    """Production entry point. Builds a TunnelManager-backed startup callback."""

    def startup(token: str) -> dict[str, Any]:
        manager = TunnelManager(schema)
        result = manager.start_all_and_build_output(pid=os.getpid(), token=token)
        if isinstance(result, OutputSchema):
            return {"kind": "success", "payload": result.model_dump(mode="json")}
        assert isinstance(result, ErrorOutput)
        manager.stop_all()
        return {"kind": "required_failure", "payload": result.model_dump(mode="json")}

    return spawn_daemon_with_callback(
        startup_callback=startup,
        log_file=schema.daemon.log_file,
    )


def spawn_daemon_with_callback(
    *,
    startup_callback: StartupCallback,
    log_file: str | None,
) -> dict[str, Any]:
    """Fork twice, run ``startup_callback`` in the final daemon, return IPC message.

    Raises ``SystemExit(2)`` if the daemon reports a required tunnel failure.
    Raises ``DaemonError`` (via ``SystemExit(4)``) if anything else fails to set
    up the daemon process.
    """
    read_fd, write_fd = os.pipe()
    runtime_token = secrets.token_urlsafe(32)

    first_pid = os.fork()
    if first_pid > 0:
        os.close(write_fd)
        return _parent_wait(read_fd, child_pid=first_pid)

    # First child.
    os.close(read_fd)
    try:
        os.setsid()
        os.umask(0)
        second_pid = os.fork()
        if second_pid > 0:
            os._exit(0)
        _final_daemon_main(write_fd, runtime_token, log_file, startup_callback)
    except DaemonError as exc:
        _write_message(write_fd, {"kind": "daemon_error", "payload": exc.to_error_output()})
        os._exit(4)
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass
    os._exit(0)


def _final_daemon_main(
    write_fd: int,
    token: str,
    log_file: str | None,
    startup_callback: StartupCallback,
) -> None:
    os.environ[TOKEN_ENV_VAR] = token
    sys.stdout.flush()
    sys.stderr.flush()

    target_path = log_file if log_file is not None else os.devnull
    try:
        target = open(target_path, "ab", buffering=0)  # noqa: SIM115
        devnull_in = open(os.devnull, "rb")  # noqa: SIM115
    except OSError as exc:
        raise DaemonError("failed to open daemon log target", {"errno": exc.errno}) from exc
    # Redirect the OS-level standard file descriptors (0, 1, 2) directly rather
    # than going through sys.stdin/stdout/stderr.fileno(). pytest's capture
    # machinery replaces sys.* with pseudo-files that lack a real fileno, but
    # the underlying OS FDs are still valid in the forked child.
    os.dup2(devnull_in.fileno(), 0)
    os.dup2(target.fileno(), 1)
    os.dup2(target.fileno(), 2)

    try:
        message = startup_callback(token)
    except Exception as exc:  # noqa: BLE001
        _write_message(
            write_fd,
            {
                "kind": "daemon_error",
                "payload": {
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "details": {},
                },
            },
        )
        os._exit(4)

    _write_message(write_fd, message)
    os.close(write_fd)

    if message["kind"] == "required_failure":
        os._exit(2)

    _install_signal_handlers()
    _wait_forever()


def _parent_wait(read_fd: int, *, child_pid: int) -> dict[str, Any]:
    try:
        with os.fdopen(read_fd, "rb") as reader:
            raw = reader.read()
    except OSError as exc:
        raise DaemonError("failed to read daemon IPC pipe", {"errno": exc.errno}) from exc
    try:
        message: dict[str, Any] = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DaemonError("daemon IPC produced invalid JSON", {"position": exc.pos}) from exc

    # Reap the immediate child of the first fork; the final daemon is reparented to PID 1.
    try:
        os.waitpid(child_pid, 0)
    except ChildProcessError:
        pass

    kind = message.get("kind")
    if kind == "success":
        return message
    if kind == "required_failure":
        # Parent re-raises as SystemExit so the CLI can pick the exit code without
        # interpreting payload structure.
        raise SystemExit(2)
    if kind == "daemon_error":
        raise SystemExit(4)
    raise DaemonError("unexpected IPC message kind", {"kind": str(kind)})


def _write_message(fd: int, message: dict[str, Any]) -> None:
    payload = (json.dumps(message) + "\n").encode("utf-8")
    while payload:
        written = os.write(fd, payload)
        if written <= 0:
            break
        payload = payload[written:]


def _install_signal_handlers() -> None:
    def handler(signum: int, _frame: object) -> None:  # noqa: ARG001
        os._exit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _wait_forever() -> None:
    event = threading.Event()
    event.wait()
