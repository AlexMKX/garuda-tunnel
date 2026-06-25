# Task A — Deterministic session reuse + single session-local lock (issue #7)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tunstrap start --session-dir <p>` reusable on a deterministic path — fail only when a daemon is *actively* on that path, otherwise reclaim the orphaned `tunnel-data/` — by replacing the per-token identity lock with a single `<session_dir>/session.lock` and removing the `token` concept entirely (no backward compatibility).

**Architecture:** One flock per session lives at `<session_dir>/session.lock`, beside (not inside) `tunnel-data/`. `SessionDir` owns the lock: `create()` acquires it `LOCK_EX|LOCK_NB`; a busy lock raises `SessionActive` (exit 3); a free lock means any pre-existing `tunnel-data/` is orphaned and is wiped+recreated. `status`/`stop` verify liveness by flock-probing the same file. `token`, `identity._state_dir`, and `daemon._sweep_stale_lockfiles` are deleted.

**Tech Stack:** Python 3.10+, Click, Pydantic v2, `fcntl.flock`, pytest. Spec: `docs/specs/2026-06-24-session-reuse-design.md`.

---

## File Structure

| File | Responsibility after this plan |
|------|--------------------------------|
| `tunstrap/exceptions.py` | Add `SessionActive` (exit 3). |
| `tunstrap/identity.py` | flock primitives on `<session_dir>/session.lock`: `acquire_session_lock`, `release_session_lock`, `verify_session`. No `_state_dir`/`verify_token`. |
| `tunstrap/session.py` | Owns root + `session.lock` + `tunnel-data/`: acquire on `create()`, reclaim orphan `tunnel-data/`, `write_identity(pid)`, `read_identity()->int`, release on `cleanup()`. |
| `tunstrap/_worker.py` | `SessionDir.create()` does the locking; map `SessionActive`→IPC `session_active`; no `--token`, no `_acquire/_release_identity_lock`. |
| `tunstrap/daemon.py` | No `secrets`/`--token`; accept IPC kind `session_active`; delete `_sweep_stale_lockfiles`. |
| `tunstrap/manager.py` | `start_all_and_build_output(pid, session_dir)` — no `token`. |
| `tunstrap/schemas.py` | `OutputSchema` without `token`. |
| `tunstrap/cli.py` | `status`/`stop` key off `--session-dir` via `verify_session`; `start` maps `session_active`→3; no `--token`. |
| `tests/**` | Rewritten to the token-free, session-lock contract. |

Ordering rationale: Task 1–2 are additive (suite stays green). Task 3 is the coordinated cut-over (token dies, lock moves) and lands with all tests updated. Task 4 deletes the now-dead legacy and proves zero residue.

---

### Task 1: Add `SessionActive` exception (exit 3)

**Files:**
- Modify: `tunstrap/exceptions.py`
- Test: `tests/unit/test_exceptions.py` (create if absent)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_exceptions.py
from tunstrap.exceptions import SessionActive, exit_code_for


def test_session_active_exit_code_is_3():
    exc = SessionActive("session already active", {"session_dir": "/tmp/x"})
    assert exit_code_for(exc) == 3
    assert exc.to_error_output()["error"] == "SessionActive"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_exceptions.py -v`
Expected: FAIL with `ImportError: cannot import name 'SessionActive'`.

- [ ] **Step 3: Add the exception and exit-code mapping**

In `tunstrap/exceptions.py`, after `class KubeParseError(...)`:

```python
class SessionActive(TunstrapError):
    """A live daemon already holds the session lock for this session dir."""
```

And add to `_EXIT_CODES`:

```python
_EXIT_CODES: dict[type[TunstrapError], int] = {
    SchemaValidationError: 1,
    RequiredTunnelFailure: 2,
    KubeParseError: 2,
    DaemonError: 4,
    SessionActive: 3,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_exceptions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tunstrap/exceptions.py tests/unit/test_exceptions.py
git commit -m "feat(exceptions): add SessionActive (exit 3)"
```

---

### Task 2: `identity.py` — session-local lock primitives

Replaces token-keyed verification with session-dir flock probing. `IdentityCheckResult` is unchanged.

**Files:**
- Rewrite: `tunstrap/identity.py`
- Rewrite: `tests/unit/test_identity.py`

- [ ] **Step 1: Write the failing tests**

Replace `tests/unit/test_identity.py` entirely:

```python
"""Session-identity verification via fcntl flock on <session_dir>/session.lock."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tunstrap.identity import (
    IdentityCheckResult,
    acquire_session_lock,
    release_session_lock,
    verify_session,
)


def _spawn_locker(session_dir: Path) -> subprocess.Popen[bytes]:
    """Child that acquires session.lock and sleeps, holding the flock."""
    code = (
        "import sys, time;"
        "from tunstrap.identity import acquire_session_lock;"
        "acquire_session_lock(sys.argv[1]);"
        "print('locked', flush=True);"
        "time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(session_dir)],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    proc.stdout.readline()  # wait for 'locked'
    return proc


def test_verify_session_match(tmp_path: Path) -> None:
    proc = _spawn_locker(tmp_path)
    try:
        assert verify_session(tmp_path, proc.pid) == IdentityCheckResult.match
    finally:
        proc.terminate()
        proc.wait()


def test_verify_session_not_found_when_no_lockfile(tmp_path: Path) -> None:
    assert verify_session(tmp_path, os.getpid()) == IdentityCheckResult.not_found


def test_verify_session_not_found_for_dead_pid(tmp_path: Path) -> None:
    assert verify_session(tmp_path, 2**31 - 1) == IdentityCheckResult.not_found


def test_verify_session_not_found_when_lock_free(tmp_path: Path) -> None:
    (tmp_path / "session.lock").write_text("12345\n")
    assert verify_session(tmp_path, 12345) == IdentityCheckResult.not_found


def test_acquire_is_mutually_exclusive(tmp_path: Path) -> None:
    fd = acquire_session_lock(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            acquire_session_lock(tmp_path)
    finally:
        release_session_lock(fd, tmp_path)


def test_release_unlinks_lockfile(tmp_path: Path) -> None:
    fd = acquire_session_lock(tmp_path)
    assert (tmp_path / "session.lock").exists()
    release_session_lock(fd, tmp_path)
    assert not (tmp_path / "session.lock").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_identity.py -v`
Expected: FAIL on imports (`acquire_session_lock` undefined).

- [ ] **Step 3: Rewrite `tunstrap/identity.py`**

```python
"""Session-identity check via fcntl.flock on ``<session_dir>/session.lock``.

The daemon acquires an exclusive flock on the session dir's ``session.lock``
at startup and holds the fd for its lifetime. ``verify_session`` consults the
same file: if it is locked and the recorded PID matches, identity is confirmed.
"""

from __future__ import annotations

import enum
import fcntl
import os
from pathlib import Path

_LOCK_NAME = "session.lock"


class IdentityCheckResult(str, enum.Enum):
    """Outcome of session verification used by stop/status."""

    # pylint: disable=invalid-name
    match = "match"
    mismatch = "mismatch"
    not_found = "not_found"
    unavailable = "unavailable"


def _lock_path(session_dir: str | Path) -> Path:
    """Return the absolute path to ``<session_dir>/session.lock``."""
    return Path(session_dir).resolve() / _LOCK_NAME


def acquire_session_lock(session_dir: str | Path) -> int:
    """Exclusively flock ``session.lock`` non-blocking; record pid; return fd.

    Raises ``BlockingIOError`` if another live process already holds it. The
    fd must stay open for the holder's lifetime; the kernel releases the flock
    automatically when the process exits, clean or not.
    """
    path = _lock_path(session_dir)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    # Truncate + write only AFTER winning the lock, so a losing racer's open()
    # can never clobber the winner's recorded pid.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    os.fsync(fd)
    return fd


def release_session_lock(lock_fd: int, session_dir: str | Path) -> None:
    """Unlink ``session.lock`` and close the fd. Best-effort; never raises."""
    try:
        _lock_path(session_dir).unlink()
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def verify_session(session_dir: str | Path, pid: int) -> IdentityCheckResult:
    """Return whether ``pid`` is alive and holds the session lock."""
    if not _process_exists(pid):
        return IdentityCheckResult.not_found
    path = _lock_path(session_dir)
    if not path.is_file():
        return IdentityCheckResult.not_found
    return _check_lock(path, pid)


def _check_lock(lock_path: Path, pid: int) -> IdentityCheckResult:
    """Determine identity from flock state and the recorded PID."""
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return IdentityCheckResult.unavailable
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Held — a daemon is alive. Verify the PID matches.
            try:
                recorded_pid = int(lock_path.read_bytes().strip())
            except (OSError, ValueError):
                return IdentityCheckResult.unavailable
            if recorded_pid != pid:
                return IdentityCheckResult.mismatch
            return IdentityCheckResult.match
        # Got the lock — no live holder. Release and report dead.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return IdentityCheckResult.not_found
    finally:
        os.close(fd)


def _process_exists(pid: int) -> bool:
    """True iff a process with the given PID currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_identity.py -v`
Expected: PASS (6 tests).

> Note: the wider suite is now red (other modules still import `verify_token`). That is fixed in Task 3. Do not run the full suite at this commit.

- [ ] **Step 5: Commit**

```bash
git add tunstrap/identity.py tests/unit/test_identity.py
git commit -m "feat(identity): session-local flock primitives (acquire/release/verify_session)"
```

---

### Task 3: Cut over to the session lock and remove `token` everywhere

This is the coordinated change. Edit all production files, then all tests, then run the full suite green. Commit once at the end.

**Files:**
- Modify: `tunstrap/session.py`, `tunstrap/_worker.py`, `tunstrap/daemon.py`, `tunstrap/manager.py`, `tunstrap/schemas.py`, `tunstrap/cli.py`
- Modify tests: `tests/conftest.py`, `tests/unit/test_cli_runner.py`, `tests/unit/test_output_schema.py`, `tests/unit/test_output_kube.py`, `tests/unit/test_manager_required.py`, and every `tests/integration/*.py` that reads `body["token"]` or stops by `--token`.

- [ ] **Step 1: `schemas.py` — drop `OutputSchema.token`**

Remove the `token: str` line (currently `schemas.py:347`) from `class OutputSchema`. Final fields:

```python
class OutputSchema(BaseModel):
    """Success envelope returned by ``tunstrap start`` on stdout."""

    model_config = ConfigDict(extra="forbid")

    connections: dict[str, NodeOutput]
    pid: int
    session_dir: str
    started_at: str
    warnings: list[TunnelWarning] = Field(default_factory=list)
```

- [ ] **Step 2: `manager.py` — drop `token` from the builder**

Change the signature (`manager.py:72-78`) and the `OutputSchema(...)` call (`manager.py:117-124`):

```python
    async def start_all_and_build_output(
        self,
        *,
        pid: int,
        session_dir: str,
    ) -> OutputSchema | ErrorOutput:
```

```python
        return OutputSchema(
            connections=connections,
            pid=pid,
            session_dir=session_dir,
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            warnings=warnings,
        )
```

- [ ] **Step 3: `session.py` — own the lock, reclaim orphans, pid-only identity**

Replace the body of `tunstrap/session.py` from the imports and class down with:

```python
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from tunstrap.exceptions import SessionActive
from tunstrap.identity import acquire_session_lock, release_session_lock

_TUNNEL_DATA = "tunnel-data"


class SessionError(Exception):
    """The session dir or its tunnel-data subdir failed validation."""


class SessionDir:
    """Owns session.lock + the tunnel-data/ subdir for one daemon instance."""

    def __init__(self, *, session_dir: Path, generated: bool, lock_fd: int) -> None:
        self.session_dir = str(session_dir)
        self._root = session_dir
        self._generated = generated
        self._data = session_dir / _TUNNEL_DATA
        self._lock_fd = lock_fd

    @classmethod
    def create(cls, *, supplied: str | None, base: Path | None = None) -> "SessionDir":
        """Resolve the session dir, acquire session.lock, (re)create tunnel-data/.

        Raises ``SessionActive`` if a live daemon already holds the lock.
        """
        if supplied is None:
            parent = base if base is not None else Path(tempfile.gettempdir())
            root = Path(tempfile.mkdtemp(prefix="tunstrap-", dir=parent))
            generated = True
        else:
            supplied_path = Path(supplied)
            if not supplied_path.is_absolute():
                raise SessionError("session dir must be an absolute path")
            root = supplied_path.resolve()
            root.mkdir(parents=True, exist_ok=True)
            generated = False

        try:
            lock_fd = acquire_session_lock(root)
        except BlockingIOError as exc:
            raise SessionActive(
                "session already active",
                {"session_dir": str(root)},
            ) from exc

        try:
            data = root / _TUNNEL_DATA
            cls._reclaim_data_slot(data)
            data.mkdir(mode=0o700)
        except BaseException:
            release_session_lock(lock_fd, root)
            raise
        return cls(session_dir=root, generated=generated, lock_fd=lock_fd)

    @staticmethod
    def _reclaim_data_slot(data: Path) -> None:
        """Wipe an orphaned tunnel-data/; reject an unsafe pre-existing slot.

        The caller holds the exclusive session.lock, so any existing tunnel-data
        belongs to a dead session and is safe to remove. Symlinks, non-dirs, and
        foreign-owned dirs are still rejected (untrusted --session-dir).
        """
        if data.is_symlink():
            raise SessionError("tunnel-data is a symlink; refusing to follow")
        if data.exists():
            if not data.is_dir():
                raise SessionError("tunnel-data exists and is not a directory")
            if data.stat().st_uid != os.getuid():
                raise SessionError("tunnel-data exists and is not owned by this user")
            shutil.rmtree(data)

    def write_identity(self, *, pid: int) -> None:
        """Write daemon.pid (mode 0600) into tunnel-data/."""
        self._write_file("daemon.pid", f"{pid}\n".encode("ascii"))

    def materialize(self, name: str, content: bytes) -> str:
        """Write `content` to tunnel-data/<name> (mode 0600); return the path."""
        return self._write_file(name, content)

    def _write_file(self, name: str, content: bytes) -> str:
        if "/" in name or "\\" in name:
            raise SessionError(f"unsafe materialized file name: {name!r}")
        if name in (".", ".."):
            raise SessionError(f"unsafe materialized file name: {name!r}")
        path = self._data / name
        if path.resolve().parent != self._data.resolve():
            raise SessionError(f"unsafe materialized file name: {name!r}")
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return str(path)

    def cleanup(self) -> None:
        """Release the lock, then remove tunnel-data/ (or the whole generated dir)."""
        release_session_lock(self._lock_fd, self._root)
        if self._generated:
            shutil.rmtree(self._root, ignore_errors=True)
        else:
            shutil.rmtree(self._data, ignore_errors=True)

    @staticmethod
    def read_identity(session_dir: str) -> int:
        """Read the recorded pid from a session dir's tunnel-data/daemon.pid."""
        data = Path(session_dir).resolve() / _TUNNEL_DATA
        try:
            return int((data / "daemon.pid").read_text().strip())
        except (OSError, ValueError) as exc:
            raise SessionError(f"cannot read identity from {data}: {exc}") from exc

    @classmethod
    def cleanup_path(cls, session_dir: str) -> None:
        """Remove <session_dir>/tunnel-data best-effort (stop-side cleanup)."""
        data = Path(session_dir).resolve() / _TUNNEL_DATA
        shutil.rmtree(data, ignore_errors=True)
```

- [ ] **Step 4: `_worker.py` — locking via SessionDir, no token, SessionActive→IPC**

Apply these edits to `tunstrap/_worker.py`:

1. Drop `import fcntl` (no longer used) and the `--token` argument:

```python
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tunstrap._worker", add_help=False)
    parser.add_argument("--ipc-fd", type=int, required=True)
    parser.add_argument("--session-dir", default=None)
    return parser.parse_args(argv)
```

2. Delete `_acquire_identity_lock` and `_release_identity_lock` entirely.

3. Add the import:

```python
from tunstrap.exceptions import DaemonError, SessionActive
```

4. Change `_run` to take only `(args, session)` and drop every `_release_identity_lock(...)` call (cleanup releases the lock). New signature + cleanup paths:

```python
async def _run(args: argparse.Namespace, session: SessionDir) -> int:
    try:
        schema = _read_schema_from_stdin()
    except (DaemonError, ValidationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _report_pre_run_failure(args.ipc_fd, exc)
        try:
            os.close(args.ipc_fd)
        except OSError:
            pass
        session.cleanup()
        return 4

    manager = TunnelManager(schema, session=session if schema.daemon.materialize else None)

    try:
        result = await manager.start_all_and_build_output(
            pid=os.getpid(), session_dir=session.session_dir
        )
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        await manager.stop_all()
        _report_pre_run_failure(args.ipc_fd, exc)
        try:
            os.close(args.ipc_fd)
        except OSError:
            pass
        session.cleanup()
        return 4

    if isinstance(result, ErrorOutput):
        await manager.stop_all()
        _write_message(
            args.ipc_fd,
            {"kind": "required_failure", "payload": result.model_dump(mode="json")},
        )
        os.close(args.ipc_fd)
        session.cleanup()
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

    idle_task: asyncio.Task[None] | None = None
    if schema.daemon.auto_stop_idle_seconds is not None:
        idle_task = asyncio.create_task(
            _idle_watchdog(
                tracker=manager.activity_tracker,
                timeout_seconds=schema.daemon.auto_stop_idle_seconds,
                stop_event=stop_event,
            )
        )

    try:
        await stop_event.wait()
    finally:
        if idle_task is not None:
            idle_task.cancel()
            try:
                await idle_task
            except asyncio.CancelledError:
                pass
        await manager.stop_all()
        session.cleanup()
    return 0
```

5. Rewrite `main` to acquire via `create()`, mapping `SessionActive` to the `session_active` IPC frame and exit 3:

```python
def main(argv: list[str] | None = None) -> None:
    """Worker entry: create+lock session dir, run loop, clean up, exit."""
    args = _parse_args(argv)
    try:
        session = SessionDir.create(supplied=args.session_dir)
    except SessionActive as exc:
        try:
            _write_message(
                args.ipc_fd,
                {"kind": "session_active", "payload": exc.to_error_output()},
            )
            os.close(args.ipc_fd)
        except OSError:
            pass
        os._exit(3)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        _report_pre_run_failure(args.ipc_fd, exc)
        os._exit(4)

    try:
        session.write_identity(pid=os.getpid())
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        session.cleanup()
        _report_pre_run_failure(args.ipc_fd, exc)
        os._exit(4)

    rc = asyncio.run(_run(args, session))
    os._exit(rc)
```

- [ ] **Step 5: `daemon.py` — no token, accept `session_active`, delete sweeper**

1. Delete the entire `_sweep_stale_lockfiles` function and its call at the top of `spawn_daemon` (`_sweep_stale_lockfiles()`).
2. Remove now-unused imports: `import fcntl`, `import secrets`, and `from tunstrap.identity import _state_dir`.
3. Remove the token: delete `runtime_token = secrets.token_urlsafe(32)` and the `f"--token={runtime_token}",` argv line.
4. Accept the new IPC kind in `_read_ipc_response`:

```python
    kind = message.get("kind")
    if kind in {"success", "required_failure", "daemon_error", "session_active"}:
        return message
    raise DaemonError("unexpected IPC message kind", {"kind": str(kind)})
```

- [ ] **Step 6: `cli.py` — verify_session, map session_active→3, no token**

1. Fix the import:

```python
from tunstrap.identity import IdentityCheckResult, verify_session
```

2. In `start_command`, add the `session_active` mapping after the `daemon_error` check:

```python
        kind = message["kind"]
        if kind == "required_failure":
            sys.exit(2)
        if kind == "daemon_error":
            sys.exit(4)
        if kind == "session_active":
            sys.exit(3)
        # kind == "success" → exit 0 (default)
```

3. Replace `stop_command`:

```python
@main.command("stop")
@click.option("--session-dir", "session_dir", required=True)
@click.option("--grace-seconds", type=int, default=10, show_default=True)
def stop_command(session_dir: str, grace_seconds: int) -> None:
    """Stop the daemon recorded under <session-dir>/tunnel-data and clean it up."""
    try:
        pid = SessionDir.read_identity(session_dir)
    except SessionError as exc:
        sys.stdout.write(json.dumps({"stopped": False, "reason": str(exc)}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(0)
        return
    _kill_with_identity(pid, grace_seconds, force=True, session_dir=session_dir)
    SessionDir.cleanup_path(session_dir)
```

4. Replace `status_command` and `_is_alive` with a session-dir-keyed status:

```python
@main.command("status")
@click.option("--session-dir", "session_dir", required=True)
def status_command(session_dir: str) -> None:
    """Report whether the daemon for the given session dir is alive."""
    try:
        pid = SessionDir.read_identity(session_dir)
    except SessionError:
        alive = False
    else:
        alive = verify_session(session_dir, pid) == IdentityCheckResult.match
    sys.stdout.write(json.dumps({"alive": alive}))
    sys.stdout.write("\n")
    sys.stdout.flush()
```

(Delete the `_is_alive` function.)

5. Replace `_kill_with_identity` to use `verify_session(session_dir, pid)` and drop the token param (rename the "token mismatch" reason to "identity mismatch"):

```python
def _kill_with_identity(  # pylint: disable=too-many-return-statements
    pid: int, grace_seconds: int, *, force: bool, session_dir: str
) -> bool:
    check = verify_session(session_dir, pid)
    if check == IdentityCheckResult.not_found:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "not found"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    if check == IdentityCheckResult.mismatch:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "identity mismatch"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    if check == IdentityCheckResult.unavailable:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "identity check unavailable"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        sys.stdout.write(json.dumps({"stopped": True}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return True

    deadline = time.monotonic() + max(0, grace_seconds)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            sys.stdout.write(json.dumps({"stopped": True}))
            sys.stdout.write("\n")
            sys.stdout.flush()
            return True
        time.sleep(0.5)

    if not force:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "still alive"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False

    recheck = verify_session(session_dir, pid)
    if recheck != IdentityCheckResult.match:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "identity changed during grace"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        sys.stdout.write(json.dumps({"stopped": True}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return True
    sys.stdout.write(json.dumps({"stopped": True, "forced": True}))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return True
```

6. Ensure `from pathlib import Path` is still needed; if `Path` is now unused in `cli.py`, remove the import.

- [ ] **Step 7: Migrate the tests (token-free contract)**

For each file, read it first, then apply the contract below. The authoritative gate is Step 8 (full suite green) + Task 4 (`rg` legacy = 0).

`tests/conftest.py` — the teardown fixture currently stops daemons by `--pid/--token`. Track session dirs instead and stop by `--session-dir`. Pattern:

```python
# started_daemons should collect session_dir strings (not (pid, token) tuples).
for session_dir in started:
    subprocess.run(
        ["tunstrap", "stop", "--session-dir", session_dir],
        check=False,
        capture_output=True,
    )
```

`tests/unit/test_output_schema.py`, `tests/unit/test_output_kube.py` — remove every `token=...` / `"token": ...` key from `OutputSchema(...)` constructions and expected dicts.

`tests/unit/test_manager_required.py` — drop the `token="t"` kwarg from all `start_all_and_build_output(pid=..., session_dir=...)` calls.

`tests/unit/test_cli_runner.py` — replace `verify_token` monkeypatches with `verify_session`, invoke `status`/`stop` with `--session-dir` only (no `--pid`/`--token`), and create session dirs that contain `tunnel-data/daemon.pid` only (no `token` file). Expected `stop` mismatch reason is now `"identity mismatch"`. Example status test:

```python
def test_status_alive_by_session_dir(monkeypatch, tmp_path):
    from tunstrap import cli as cli_mod
    from tunstrap.identity import IdentityCheckResult

    data = tmp_path / "tunnel-data"
    data.mkdir()
    (data / "daemon.pid").write_text(f"{os.getpid()}\n")
    monkeypatch.setattr(
        cli_mod, "verify_session", lambda sd, pid: IdentityCheckResult.match
    )
    out = json.loads(
        CliRunner().invoke(main, ["status", "--session-dir", str(tmp_path)]).output
    )
    assert out == {"alive": True}
```

`tests/integration/*.py` — for each file:
- `start` no longer returns `token`; delete `assert body["token"]`.
- Replace `started_daemons.append((body["pid"], body["token"]))` with `started_daemons.append(<session_dir used for that start>)` to match the new conftest teardown.
- Stops/status that used `--pid/--token` become `--session-dir <sd>`.
- `tests/integration/test_auto_stop.py`: replace `from tunstrap.identity import _state_dir` and the `_state_dir()/f"{token}.lock"` assertion with a check on `<session_dir>/session.lock` (after graceful auto-stop it must NOT exist; while running it must exist and be flock-held).
- `tests/integration/test_status.py`: replace the "wrong token" case with a "stale/dead session" case (status against a session dir whose daemon was stopped → `alive: False`).

- [ ] **Step 8: Run the full suite green**

Run: `pytest tests/unit -v` then `pytest tests/integration -m integration -v` (integration needs Docker).
Expected: PASS. Fix any remaining references until green.

- [ ] **Step 9: Commit**

```bash
git add tunstrap tests
git commit -m "feat(session): single session.lock + deterministic reuse; remove token (#7)"
```

---

### Task 4: Prove no legacy remains

**Files:** none (verification + any residue cleanup).

- [ ] **Step 1: Grep for legacy symbols**

Run:

```bash
rg -n 'verify_token|_state_dir|_sweep_stale_lockfiles|_acquire_identity_lock|_release_identity_lock|token_urlsafe' tunstrap tests
```

Expected: **no matches**. If any remain, remove them (they are dead) and re-run.

- [ ] **Step 2: Grep for residual `token`**

Run:

```bash
rg -n -i 'token' tunstrap tests
```

Expected: no occurrences in `tunstrap/` source. In `tests/` only unrelated matches are allowed (there should be none for the session/identity feature). Remove any leftover session-token references.

- [ ] **Step 3: Lint/type gates**

Run:

```bash
ruff check . && mypy --strict tunstrap/ && pylint tunstrap/ && vulture tunstrap/ vulture_whitelist.py
```

Expected: clean. Remove any now-dead entries from `vulture_whitelist.py` that referenced removed symbols.

- [ ] **Step 4: Full suite**

Run: `pytest tests/unit -v && pytest tests/integration -m integration -v`
Expected: PASS.

- [ ] **Step 5: Commit (only if Step 1–3 required edits)**

```bash
git add -A
git commit -m "chore: remove dead token/state-dir/sweeper residue"
```

---

## Self-Review

**Spec coverage:**
- Single session-local lock `<session_dir>/session.lock` → Task 2 (primitives) + Task 3 Step 3 (SessionDir owns it). ✓
- Fail only when active; otherwise reclaim → Task 3 Step 3 `_reclaim_data_slot` + `acquire_session_lock`. ✓
- Concurrency race-free → flock `LOCK_EX|LOCK_NB`; truncate-after-lock (identity Step 3) avoids pid clobber; test `test_acquire_is_mutually_exclusive`. ✓
- Exit code 3 + IPC `session_active` → Task 1, Task 3 Steps 4–6. ✓
- Remove `token` (OutputSchema/IPC/CLI/worker/manager/session) → Task 3 Steps 1–7. ✓
- Remove `_state_dir` + `_sweep_stale_lockfiles` → Task 3 Step 5 + Task 4. ✓
- `verify_token`→`verify_session` → Task 2 + Task 3 Step 6. ✓
- Safety invariants preserved (symlink/non-dir/foreign owner) → `_reclaim_data_slot`. ✓
- Tests (unit concurrency, reclaim; integration reuse, exit 3, stale reclaim) → Task 2 + Task 3 Step 7 + Task 4. ✓

**Placeholder scan:** Production code is complete. Test migration for `tests/integration/*` and some unit tests is specified as a precise contract + worked examples rather than full transcription, gated by the Step 8 full-suite-green and Task 4 `rg`-legacy-zero checks — there is no "TODO/implement later"; the gates force completeness.

**Type consistency:** `acquire_session_lock(session_dir)->int`, `release_session_lock(fd, session_dir)`, `verify_session(session_dir, pid)->IdentityCheckResult`, `SessionDir.read_identity(session_dir)->int`, `start_all_and_build_output(pid, session_dir)`, `_kill_with_identity(pid, grace_seconds, *, force, session_dir)` — names/signatures match across tasks. ✓
