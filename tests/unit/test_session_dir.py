"""Session-directory resolution, tunnel-data creation, and cleanup.

Validates: a generated dir is removed wholesale; a supplied dir keeps
only tunnel-data removed; tunnel-data is 0700; an existing/symlinked
tunnel-data is rejected.
Code: tunstrap/session.py
Assertion: directory existence/mode after open/cleanup matches the rules;
SessionError is raised on a hostile tunnel-data.
Method: drive SessionDir against tmp_path with crafted preconditions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tunstrap.session import SessionDir, SessionError

pytestmark = pytest.mark.unit


def test_generated_dir_cleanup_removes_whole_dir(tmp_path: Path) -> None:
    """A daemon-generated session dir is removed entirely on cleanup."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    root = Path(sd.session_dir)
    assert (root / "tunnel-data").is_dir()
    sd.cleanup()
    assert not root.exists()


def test_supplied_dir_cleanup_keeps_dir(tmp_path: Path) -> None:
    """A supplied session dir keeps the dir; only tunnel-data is removed."""
    supplied = tmp_path / "work"
    supplied.mkdir()
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    assert (supplied / "tunnel-data").is_dir()
    sd.cleanup()
    assert supplied.exists()
    assert not (supplied / "tunnel-data").exists()


def test_tunnel_data_is_0700(tmp_path: Path) -> None:
    """tunnel-data is created with mode 0700."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    mode = stat.S_IMODE(os.stat(Path(sd.session_dir) / "tunnel-data").st_mode)
    assert mode == 0o700


def test_reclaims_existing_tunnel_data(tmp_path: Path) -> None:
    """A pre-existing owned tunnel-data (orphan) is wiped and recreated fresh.

    With the single session.lock held exclusively, any leftover tunnel-data
    belongs to a dead session and is safe to reclaim.
    """
    supplied = tmp_path / "work"
    data = supplied / "tunnel-data"
    data.mkdir(parents=True)
    (data / "leftover").write_text("stale\n")
    sd = SessionDir.create(supplied=str(supplied), base=tmp_path)
    assert (supplied / "tunnel-data").is_dir()
    assert not (supplied / "tunnel-data" / "leftover").exists()
    sd.cleanup()


def test_rejects_symlink_tunnel_data(tmp_path: Path) -> None:
    """A symlinked tunnel-data is rejected (no symlink-following)."""
    supplied = tmp_path / "work"
    supplied.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (supplied / "tunnel-data").symlink_to(target)
    with pytest.raises(SessionError):
        SessionDir.create(supplied=str(supplied), base=tmp_path)


def test_write_identity_and_materialize(tmp_path: Path) -> None:
    """Identity files and a materialized file land in tunnel-data, mode 0600."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    sd.write_identity(pid=4321)
    path = sd.materialize("hub-k3s", b"kubeconfig-bytes")
    data_dir = Path(sd.session_dir) / "tunnel-data"
    assert (data_dir / "daemon.pid").read_text().strip() == "4321"
    assert Path(path).read_bytes() == b"kubeconfig-bytes"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_write_file_rejects_traversal_name(tmp_path: Path) -> None:
    """materialize() with a traversal name is rejected (defense in depth)."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    with pytest.raises(SessionError):
        sd.materialize("../escaped", b"x")


def test_write_file_rejects_slash_name(tmp_path: Path) -> None:
    """materialize() with a nested path is rejected."""
    sd = SessionDir.create(supplied=None, base=tmp_path)
    with pytest.raises(SessionError):
        sd.materialize("sub/dir", b"x")


def test_rejects_relative_supplied_dir(tmp_path: Path) -> None:
    """A relative --session-dir is rejected before resolution."""
    with pytest.raises(SessionError):
        SessionDir.create(supplied="relative-session", base=tmp_path)


def test_accepts_absolute_supplied_dir(tmp_path: Path) -> None:
    """An absolute --session-dir is accepted (regression guard)."""
    abs_dir = tmp_path / "work"
    sd = SessionDir.create(supplied=str(abs_dir), base=tmp_path)
    assert Path(sd.session_dir) == abs_dir.resolve()
