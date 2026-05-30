"""Stateless PID + token identity check via fcntl.flock on a per-token lockfile.

The daemon acquires an exclusive flock on ``<state_dir>/<token>.lock`` at
startup and holds the fd for its lifetime. ``verify_token`` consults the
same file: if it is locked and the recorded PID matches, identity is
confirmed.
"""

from __future__ import annotations

import enum
import fcntl
import os
from pathlib import Path

_STATE_SUBDIR = "garuda-tunnel"


class IdentityCheckResult(str, enum.Enum):
    """Outcome of pid+token verification used by stop/status."""

    # Enum members are serialised verbatim via str inheritance; keep lowercase
    # so JSON output and the equality short-circuits stay readable.
    # pylint: disable=invalid-name
    match = "match"
    mismatch = "mismatch"
    not_found = "not_found"
    unavailable = "unavailable"


def _state_dir() -> Path:
    """Return the per-user state directory for garuda-tunnel runtime files.

    Honours ``XDG_STATE_HOME``; falls back to ``~/.local/state``. Caller is
    responsible for creating the directory if it does not yet exist.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / _STATE_SUBDIR


def verify_token(pid: int, token: str, state_dir: Path | None = None) -> IdentityCheckResult:
    """Return whether ``pid`` is alive and owns the identity lock for ``token``."""
    if not _process_exists(pid):
        return IdentityCheckResult.not_found

    base = state_dir if state_dir is not None else _state_dir()
    lock_path = base / f"{token}.lock"
    if not lock_path.is_file():
        return IdentityCheckResult.not_found

    return _check_lock(lock_path, pid)


def _check_lock(lock_path: Path, pid: int) -> IdentityCheckResult:
    """Open ``lock_path`` and determine identity from flock state and recorded PID."""
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return IdentityCheckResult.unavailable
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Lock is held — daemon is alive. Verify the PID matches.
            try:
                recorded_pid = int(lock_path.read_bytes().strip())
            except (OSError, ValueError):
                return IdentityCheckResult.unavailable
            if recorded_pid != pid:
                return IdentityCheckResult.mismatch
            return IdentityCheckResult.match
        # Got the lock — daemon is dead. Release immediately and report.
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
        # Process exists but is owned by someone else.
        return True
    return True
