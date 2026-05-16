"""Stateless PID + token identity check used by `stop` and `status`."""

from __future__ import annotations

import enum
import os
import subprocess
import sys
from pathlib import Path

TOKEN_ENV_VAR = "GARUDA_TUNNEL_TOKEN"


class IdentityCheckResult(str, enum.Enum):
    match = "match"
    mismatch = "mismatch"
    not_found = "not_found"
    unavailable = "unavailable"


def verify_token(pid: int, token: str) -> IdentityCheckResult:
    """Return whether ``pid`` is alive and carries ``GARUDA_TUNNEL_TOKEN=token``."""
    if not _process_exists(pid):
        return IdentityCheckResult.not_found

    found = _read_token_env(pid)
    if found is None:
        return IdentityCheckResult.unavailable
    return IdentityCheckResult.match if found == token else IdentityCheckResult.mismatch


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else.
        return True
    return True


def _read_token_env(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        return _read_token_env_linux(pid)
    if sys.platform == "darwin":
        return _read_token_env_macos(pid)
    return None


def _read_token_env_linux(pid: int) -> str | None:
    environ_path = Path(f"/proc/{pid}/environ")
    try:
        data = environ_path.read_bytes()
    except (FileNotFoundError, PermissionError):
        return None
    prefix = f"{TOKEN_ENV_VAR}=".encode()
    for entry in data.split(b"\0"):
        if entry.startswith(prefix):
            return entry[len(prefix) :].decode("utf-8", errors="replace")
    return None


def _read_token_env_macos(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-wwE", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    needle = f"{TOKEN_ENV_VAR}="
    for token_part in result.stdout.split():
        if token_part.startswith(needle):
            return token_part[len(needle) :]
    return None
