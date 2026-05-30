"""Session directory: identity + optional materialized files under tunnel-data/.

The daemon always works inside a well-known `tunnel-data/` subdirectory of
the session dir. When the daemon generates the session dir itself, cleanup
removes the whole dir; when the caller supplies it, cleanup removes only
`tunnel-data/` (the caller's directory is never touched). `--session-dir`
is untrusted: an existing tunnel-data that is a symlink, a non-directory,
or not owned by the current user is rejected.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TUNNEL_DATA = "tunnel-data"


class SessionError(Exception):
    """The session dir or its tunnel-data subdir failed validation."""


class SessionDir:
    """Owns the tunnel-data/ subdir lifecycle for one daemon instance."""

    def __init__(self, *, session_dir: Path, generated: bool) -> None:
        """Store the resolved session dir and whether the daemon generated it."""
        self.session_dir = str(session_dir)
        self._root = session_dir
        self._generated = generated
        self._data = session_dir / _TUNNEL_DATA

    @classmethod
    def create(cls, *, supplied: str | None, base: Path | None = None) -> "SessionDir":
        """Resolve/validate the session dir and create tunnel-data/ (0700)."""
        if supplied is None:
            parent = base if base is not None else Path(tempfile.gettempdir())
            root = Path(tempfile.mkdtemp(prefix="garuda-tunnel-", dir=parent))
            generated = True
        else:
            root = Path(supplied).resolve()
            if not root.is_absolute():
                raise SessionError("session dir must be absolute")
            root.mkdir(parents=True, exist_ok=True)
            generated = False

        data = root / _TUNNEL_DATA
        cls._validate_data_slot(data)
        data.mkdir(mode=0o700)
        return cls(session_dir=root, generated=generated)

    @staticmethod
    def _validate_data_slot(data: Path) -> None:
        """Reject a pre-existing tunnel-data that is unsafe to own."""
        if data.is_symlink():
            raise SessionError("tunnel-data is a symlink; refusing to follow")
        if data.exists():
            if not data.is_dir():
                raise SessionError("tunnel-data exists and is not a directory")
            if data.stat().st_uid != os.getuid():
                raise SessionError("tunnel-data exists and is not owned by this user")
            raise SessionError(
                "tunnel-data already exists (possible orphaned session); "
                "remove it before reusing this session dir"
            )

    def write_identity(self, *, pid: int, token: str) -> None:
        """Write daemon.pid and token (mode 0600) into tunnel-data/."""
        self._write_file("daemon.pid", f"{pid}\n".encode("ascii"))
        self._write_file("token", f"{token}\n".encode("ascii"))

    def materialize(self, name: str, content: bytes) -> str:
        """Write `content` to tunnel-data/<name> (mode 0600); return the path."""
        return self._write_file(name, content)

    def _write_file(self, name: str, content: bytes) -> str:
        if "/" in name or "\\" in name or name in (".", "..") or name.startswith((".", "..")):
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
        """Remove tunnel-data/ (and the whole dir if we generated it). Best-effort."""
        if self._generated:
            shutil.rmtree(self._root, ignore_errors=True)
        else:
            shutil.rmtree(self._data, ignore_errors=True)

    @staticmethod
    def read_identity(session_dir: str) -> tuple[int, str]:
        """Read (pid, token) from a session dir's tunnel-data/. Raises SessionError."""
        data = Path(session_dir).resolve() / _TUNNEL_DATA
        try:
            pid = int((data / "daemon.pid").read_text().strip())
            token = (data / "token").read_text().strip()
        except (OSError, ValueError) as exc:
            raise SessionError(f"cannot read identity from {data}: {exc}") from exc
        return pid, token

    @classmethod
    def cleanup_path(cls, session_dir: str) -> None:
        """Remove <session_dir>/tunnel-data best-effort (stop-side cleanup)."""
        data = Path(session_dir).resolve() / _TUNNEL_DATA
        shutil.rmtree(data, ignore_errors=True)
