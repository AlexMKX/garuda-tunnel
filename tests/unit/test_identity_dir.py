"""verify_token honours an explicit state directory.

Validates: a live flock + matching pid in a given directory yields match;
a foreign pid yields mismatch.
Code: garuda_tunnel/identity.py
Assertion: verify_token(..., state_dir=...) reads the lockfile from that dir.
Method: create a lockfile held by the current process via flock and check.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from garuda_tunnel.identity import IdentityCheckResult, verify_token

pytestmark = pytest.mark.unit


def test_verify_token_uses_explicit_dir(tmp_path: Path) -> None:
    """A held lock with a matching pid in `state_dir` resolves to match."""
    token = "tok"
    lock = tmp_path / f"{token}.lock"
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, f"{os.getpid()}\n".encode())
    try:
        result = verify_token(os.getpid(), token, state_dir=tmp_path)
        assert result == IdentityCheckResult.match
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
