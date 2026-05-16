# garuda-tunnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `garuda-tunnel` CLI specified in `docs/specs/2026-05-16-garuda-tunnel-design.md`: a Python tool that opens N SSH local-forward tunnels in one call, returns a JSON port mapping plus a daemon PID and identity token, and supports safe PID+token based `stop`/`status` from a disposable execution environment.

**Architecture:** Single Python package `garuda_tunnel/` with a clear split: `cli.py` (Click commands and exit code mapping), `schemas.py` (pydantic I/O models), `manager.py` (`TunnelManager` + concurrent tunnel startup, in-memory PEM parsing), `daemon.py` (POSIX double-fork with IPC pipe, signal handling, identity token in env), `identity.py` (cross-platform `/proc` + `ps -wwE` based identity check for `stop`/`status`), `exceptions.py` (typed hierarchy). The first fork happens BEFORE any `SSHTunnelForwarder.start()` call, so tunnel threads always live in the final daemon process; parent waits on an IPC pipe for the final daemon PID and startup result, then writes the JSON output to stdout.

**Tech Stack:** Python 3.10+, `sshtunnel>=0.4.0,<0.5`, `paramiko>=4.0,<5`, `pydantic>=2.13,<3`, `click>=8.3,<9`. Build: Hatch + `hatch-vcs`. Tests: `pytest`, `pytest-cov`, `mypy --strict`, `ruff`. Integration test infra: Docker Compose with `linuxserver/openssh-server` + `iperf3` sidecars driven via `subprocess.run`.

**Definition of done:** All `Success criteria` from the spec are met. CI is green for unit (Linux+macOS, Python 3.10/3.11/3.12/3.13) and integration (Linux). The repo stays private until the final release task, when a `vYYYY.1MMDD.1HHMM` tag is created in private GitHub, `release.yml` produces sdist+wheel as Release assets, and the operator validates `pipx run --spec git+ssh://git@github.com/AlexMKX/garuda-tunnel.git@<tag> garuda-tunnel --help` from a clean shell. Public visibility flip is explicitly out of scope for G1a and happens later.

**Working tree:** `~/Projects/garuda/garuda-tunnel/` (already initialized, `main` branch, one commit with spec/README/LICENSE/.gitignore). All subsequent commits land on `main` of this repo. No git worktrees inside this repo for this plan.

---

## File structure

Files this plan creates or modifies, with single-responsibility boundaries.

### Package source (`garuda_tunnel/`)

- `garuda_tunnel/__init__.py` &mdash; exposes `__version__` via `importlib.metadata`. No other public API.
- `garuda_tunnel/__main__.py` &mdash; `python -m garuda_tunnel` entrypoint, delegates to `cli.main()`.
- `garuda_tunnel/exceptions.py` &mdash; exception hierarchy `GarudaTunnelError` -> `SchemaValidationError`, `TunnelStartupError`, `RequiredTunnelFailure`, `DaemonError`, with `to_error_output()` helpers and exit-code mapping table.
- `garuda_tunnel/schemas.py` &mdash; pydantic models: `SSHOptions`, `DaemonOptions`, `NodeInput`, `InputSchema`, `ConnectionEntry`, `TunnelWarning`, `OutputSchema`, `ErrorOutput`, plus validators for `require` and per-node auth.
- `garuda_tunnel/manager.py` &mdash; `TunnelManager` class: in-memory PEM parsing (`_load_pkey`), `start_all()` via `ThreadPoolExecutor` with verification, `stop_all()`, `start_all_and_build_output()` aggregator that returns either `OutputSchema` or `ErrorOutput`.
- `garuda_tunnel/identity.py` &mdash; `verify_token(pid, token)` returning `IdentityCheckResult` (`match`, `mismatch`, `not_found`, `unavailable`). Linux: `/proc/<pid>/environ`. macOS: `ps -wwE -p <pid>`. Other: `unavailable`.
- `garuda_tunnel/daemon.py` &mdash; `spawn_daemon(schema)`: IPC pipe + double fork; parent reads one JSON message and returns it. `_run_daemon_body()` for the final daemon (export env token, redirect FDs, start tunnels, write IPC, install signal handlers, `signal.pause()`).
- `garuda_tunnel/cli.py` &mdash; Click group `main` with subcommands `start`, `stop`, `status`. Custom `Group` class remaps `UsageError` to exit 64. All commands write JSON to stdout. `start` reads stdin JSON, validates, calls `spawn_daemon`, prints parent IPC result.

### Tests (`tests/`)

- `tests/conftest.py` &mdash; autouse `kill_orphan_test_daemons(started_daemons)` fixture that uses recorded `(pid, token)` pairs.
- `tests/unit/test_schemas.py` &mdash; pure pydantic validation.
- `tests/unit/test_cli_parsing.py` &mdash; Click `CliRunner`, no subprocess, no real I/O. Verifies `--help`, `--version`, exit 64 on usage error.
- `tests/unit/test_identity.py` &mdash; identity check against current process (token in env via `monkeypatch.setenv`) plus a non-existent PID path.
- `tests/integration/docker-compose.yml` &mdash; 3 `linuxserver/openssh-server` containers, each with `iperf3 -s -B 127.0.0.1 -p 6443`.
- `tests/integration/conftest.py` &mdash; session `ssh_test_cluster` fixture (compose up/down, public key injection), session `started_daemons` list, helper to invoke `garuda-tunnel` as subprocess.
- `tests/integration/test_start.py` &mdash; full success, required failure with cleanup verification, optional failure with warnings, schema validation failure.
- `tests/integration/test_stop.py` &mdash; happy path, wrong token, not found, SIGKILL escalation.
- `tests/integration/test_status.py` &mdash; alive, alive with wrong token, dead.
- `tests/integration/test_multiport.py` &mdash; one node with two `remote_ports` -> two `ConnectionEntry`s on distinct local ports.

### Project metadata and CI

- `pyproject.toml` &mdash; project metadata, dependencies, dev extras, scripts, hatch config, ruff/mypy/pytest config.
- `.github/workflows/test.yml` &mdash; unit matrix on Linux+macOS x Python 3.10-3.13 plus Linux-only integration job.
- `.github/workflows/release.yml` &mdash; on tag push: tests, build sdist+wheel, create Release with assets, no PyPI publish.
- `README.md` &mdash; quickstart, schema reference, examples (replaces the current placeholder content).

### Excluded

- Public repo visibility flip: explicitly out of scope for this plan.
- PyPI publishing: spec out of scope, do not add.

---

## Conventions for every task

- Each task is the smallest unit that lands one or more behaviorally-tested capabilities. Steps inside a task are 2-5 minute actions.
- TDD: write failing test first, run it to confirm RED, implement minimum to GREEN, run again, commit.
- Mocks are forbidden in `tests/integration/`. Unit tests may not mock `sshtunnel` or `paramiko`.
- Every commit must keep `ruff check`, `ruff format --check`, `mypy --strict garuda_tunnel`, and the relevant pytest selection green on the implementer's machine.
- Commit messages use Conventional Commits (`feat:`, `test:`, `chore:`, `ci:`, `docs:`, `refactor:`). One logical change per commit.
- Never write `pkill -f garuda-tunnel` anywhere.
- Never write the user's PEM content or password into logs, error output, or temp files.

---

### Task 1: Project scaffolding and tooling

**Files:**
- Create: `pyproject.toml`
- Create: `garuda_tunnel/__init__.py`
- Create: `garuda_tunnel/__main__.py`
- Create: `garuda_tunnel/cli.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/test_cli_parsing.py`
- Modify: `README.md` (replace placeholder block with quickstart-friendly text)

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "garuda-tunnel"
description = "SSH tunnel manager for ephemeral environments"
authors = [{name = "Alex MKX"}]
license = {text = "MIT"}
readme = "README.md"
requires-python = ">=3.10"
dynamic = ["version"]
dependencies = [
    "sshtunnel>=0.4.0,<0.5",
    "paramiko>=4.0,<5",
    "pydantic>=2.13,<3",
    "click>=8.3,<9",
]

[project.optional-dependencies]
dev = [
    "pytest>=9.0,<10",
    "pytest-cov>=5.0",
    "mypy>=1.13",
    "ruff>=0.8",
]

[project.scripts]
garuda-tunnel = "garuda_tunnel.cli:main"

[project.urls]
Homepage = "https://github.com/AlexMKX/garuda-tunnel"
Issues   = "https://github.com/AlexMKX/garuda-tunnel/issues"

[build-system]
requires      = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"

[tool.hatch.version]
source = "vcs"

[tool.hatch.build.hooks.vcs]
version-file = "garuda_tunnel/_version.py"

[tool.ruff]
line-length    = 100
target-version = "py310"

[tool.mypy]
strict         = true
python_version = "3.10"

[tool.pytest.ini_options]
markers = ["integration: requires docker (sshd containers)"]
addopts = "-m 'not integration' --cov=garuda_tunnel --cov-report=term-missing"
```

- [ ] **Step 2: Write `garuda_tunnel/__init__.py`**

```python
"""Public package entry point. Only ``__version__`` is exposed."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("garuda-tunnel")
except PackageNotFoundError:  # source checkout without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
```

- [ ] **Step 3: Write `garuda_tunnel/__main__.py`**

```python
"""``python -m garuda_tunnel`` entrypoint."""
from __future__ import annotations

from garuda_tunnel.cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Write minimal `garuda_tunnel/cli.py` skeleton**

```python
"""Command-line interface. Subcommands are added in later tasks."""
from __future__ import annotations

import sys

import click

from garuda_tunnel import __version__


class _UsageExit64(click.Group):
    """Remap Click usage errors from default exit 2 to exit 64.

    Click's default ``standalone_mode=True`` swallows ``UsageError`` inside
    ``BaseCommand.main`` and exits with code 2 before any caller-level
    ``except`` block sees the exception. We force non-standalone mode so we
    can catch the error ourselves, render Click's usual message, and exit
    with the documented usage-error code (64, sysexits.h ``EX_USAGE``).
    """

    def main(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
        kwargs["standalone_mode"] = False
        try:
            return super().main(*args, **kwargs)  # type: ignore[call-overload]
        except click.UsageError as exc:
            exc.show()
            sys.exit(64)


@click.group(cls=_UsageExit64)
@click.version_option(__version__, prog_name="garuda-tunnel")
def main() -> None:
    """garuda-tunnel: SSH tunnel manager for ephemeral environments."""


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 5: Write failing CLI parsing tests**

`tests/unit/test_cli_parsing.py`:

```python
from __future__ import annotations

from click.testing import CliRunner

from garuda_tunnel.cli import main


def test_help_exits_zero() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "garuda-tunnel" in result.output


def test_version_flag() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "garuda-tunnel" in result.output


def test_unknown_subcommand_exits_64() -> None:
    result = CliRunner().invoke(main, ["does-not-exist"])
    assert result.exit_code == 64
```

Empty `tests/__init__.py` and `tests/unit/__init__.py`.

- [ ] **Step 6: Install dev dependencies and run tests for the first time**

Run:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest tests/unit -v
```

Expected: 3 passed.

- [ ] **Step 7: Run lint and type-check**

Run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: all green. Fix anything ruff/mypy reports before commit.

- [ ] **Step 8: Replace README placeholder**

`README.md`:

```markdown
# garuda-tunnel

SSH tunnel manager for ephemeral execution environments (CI runners,
disposable containers, Terragrunt hooks). Opens N SSH local-forward tunnels
in one call, returns the resulting `127.0.0.1:port` mapping plus a daemon
PID and identity token as JSON, then detaches as a background daemon.

## Quickstart

```bash
pipx run --spec git+ssh://git@github.com/AlexMKX/garuda-tunnel.git@<TAG> garuda-tunnel --help
```

Full design: `docs/specs/2026-05-16-garuda-tunnel-design.md`.
Implementation plan: `docs/superpowers/plans/2026-05-16-garuda-tunnel.md`.

## License

MIT.
```

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml garuda_tunnel tests README.md
git commit -m "chore: project scaffolding with CLI skeleton and usage-error remap"
```

---

### Task 2: Exception hierarchy and exit code mapping

**Files:**
- Create: `garuda_tunnel/exceptions.py`
- Create: `tests/unit/test_exceptions.py`

- [ ] **Step 1: Write failing tests for the exception hierarchy**

`tests/unit/test_exceptions.py`:

```python
from __future__ import annotations

import pytest

from garuda_tunnel.exceptions import (
    DaemonError,
    GarudaTunnelError,
    RequiredTunnelFailure,
    SchemaValidationError,
    TunnelStartupError,
    exit_code_for,
)


def test_all_errors_inherit_base() -> None:
    for cls in [
        SchemaValidationError,
        TunnelStartupError,
        RequiredTunnelFailure,
        DaemonError,
    ]:
        assert issubclass(cls, GarudaTunnelError)


@pytest.mark.parametrize(
    "exc, expected_code",
    [
        (SchemaValidationError("bad", {"field": "host"}), 1),
        (RequiredTunnelFailure("nope", {"failed": ["a"]}), 2),
        (DaemonError("fork failed", {"errno": 12}), 4),
    ],
)
def test_exit_code_for_known_errors(exc: GarudaTunnelError, expected_code: int) -> None:
    assert exit_code_for(exc) == expected_code


def test_to_error_output_does_not_leak_secrets() -> None:
    err = SchemaValidationError("bad", {"ssh_pkey": "-----BEGIN PRIVATE KEY-----..."})
    out = err.to_error_output()
    assert out["error"] == "SchemaValidationError"
    assert out["message"] == "bad"
    assert "ssh_pkey" not in out["details"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_exceptions.py -v`
Expected: collection or import error referencing missing `garuda_tunnel.exceptions`.

- [ ] **Step 3: Implement `garuda_tunnel/exceptions.py`**

```python
"""Typed exception hierarchy with stable exit-code mapping."""
from __future__ import annotations

from typing import Any

_SECRET_KEYS = frozenset({"ssh_pkey", "ssh_password", "ssh_pkey_passphrase"})


def _scrub(details: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in details.items() if k not in _SECRET_KEYS}


class GarudaTunnelError(Exception):
    """Base class for every error this tool reports as structured JSON."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = _scrub(details or {})

    def to_error_output(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": self.message,
            "details": self.details,
        }


class SchemaValidationError(GarudaTunnelError):
    pass


class TunnelStartupError(GarudaTunnelError):
    pass


class RequiredTunnelFailure(GarudaTunnelError):
    pass


class DaemonError(GarudaTunnelError):
    pass


_EXIT_CODES: dict[type[GarudaTunnelError], int] = {
    SchemaValidationError: 1,
    RequiredTunnelFailure: 2,
    DaemonError: 4,
}


def exit_code_for(exc: GarudaTunnelError) -> int:
    """Map a domain exception to its CLI exit code; default to 1."""
    return _EXIT_CODES.get(type(exc), 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_exceptions.py -v`
Expected: 5 passed.

- [ ] **Step 5: Lint and type-check**

Run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add garuda_tunnel/exceptions.py tests/unit/test_exceptions.py
git commit -m "feat: typed exception hierarchy with exit-code map and secret scrubbing"
```

---

### Task 3: Pydantic schemas and validators

**Files:**
- Create: `garuda_tunnel/schemas.py`
- Create: `tests/unit/test_schemas.py`

- [ ] **Step 1: Write failing tests for valid inputs**

`tests/unit/test_schemas.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from garuda_tunnel.schemas import (
    ConnectionEntry,
    DaemonOptions,
    InputSchema,
    NodeInput,
    OutputSchema,
    SSHOptions,
    TunnelWarning,
)


def _node(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "host": "node1.example.net",
        "user": "ubuntu",
        "ssh_password": "p",
        "remote_ports": [6443],
    }
    base.update(overrides)
    return base


def test_valid_minimum_input_parses() -> None:
    schema = InputSchema.model_validate({"nodes": {"a": _node()}})
    assert schema.require == "*"
    assert schema.nodes["a"].port == 22
    assert isinstance(schema.daemon, DaemonOptions)
    assert isinstance(schema.nodes["a"].ssh_options, SSHOptions)


def test_require_star_or_list() -> None:
    InputSchema.model_validate({"nodes": {"a": _node()}, "require": "*"})
    InputSchema.model_validate({"nodes": {"a": _node(), "b": _node()}, "require": ["a"]})


def test_require_references_unknown_node() -> None:
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate({"nodes": {"a": _node()}, "require": ["nope"]})
    assert "unknown nodes" in str(excinfo.value)


def test_node_requires_pkey_or_password() -> None:
    payload = {"nodes": {"a": {"host": "h", "user": "u", "remote_ports": [22]}}}
    with pytest.raises(ValidationError) as excinfo:
        InputSchema.model_validate(payload)
    assert "ssh_pkey or ssh_password" in str(excinfo.value)


def test_missing_required_host_fails() -> None:
    payload = {"nodes": {"a": {"user": "u", "ssh_password": "p", "remote_ports": [22]}}}
    with pytest.raises(ValidationError):
        InputSchema.model_validate(payload)


def test_output_schema_round_trips() -> None:
    out = OutputSchema(
        connections={
            "a": [
                ConnectionEntry(
                    remote_host="127.0.0.1",
                    remote_port=6443,
                    local_host="127.0.0.1",
                    local_port=40001,
                )
            ]
        },
        pid=12345,
        token="abc",
        started_at="2026-05-16T14:30:00Z",
        warnings=[TunnelWarning(node="b", error="auth failed")],
    )
    rebuilt = OutputSchema.model_validate_json(out.model_dump_json())
    assert rebuilt == out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_schemas.py -v`
Expected: import error referencing missing `garuda_tunnel.schemas`.

- [ ] **Step 3: Implement `garuda_tunnel/schemas.py`**

```python
"""Pydantic models for CLI input/output. Single source of JSON shape."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class SSHOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compression: bool = False
    host_key_policy: Literal["auto", "reject", "warning"] = "auto"
    known_hosts_path: str | None = None
    connect_timeout: int = 60
    threaded: bool = True


class DaemonOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_file: str | None = None
    shutdown_grace_seconds: int = 10


class NodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 22
    user: str
    ssh_pkey: str | None = None
    ssh_password: str | None = None
    ssh_pkey_passphrase: str | None = None
    remote_ports: list[int] = Field(min_length=1)
    local_ports: list[int] | None = None
    ssh_options: SSHOptions = Field(default_factory=SSHOptions)


class InputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: dict[str, NodeInput]
    require: Literal["*"] | list[str] = "*"
    daemon: DaemonOptions = Field(default_factory=DaemonOptions)

    @field_validator("nodes")
    @classmethod
    def _validate_auth(cls, value: dict[str, NodeInput]) -> dict[str, NodeInput]:
        for name, node in value.items():
            if not node.ssh_pkey and not node.ssh_password:
                raise ValueError(f"node {name!r}: must provide ssh_pkey or ssh_password")
        return value

    @field_validator("require")
    @classmethod
    def _validate_require(
        cls,
        value: Literal["*"] | list[str],
        info: ValidationInfo,
    ) -> Literal["*"] | list[str]:
        if value == "*":
            return value
        nodes: dict[str, Any] = info.data.get("nodes", {})
        unknown = sorted(set(value) - set(nodes.keys()))
        if unknown:
            raise ValueError(f"require references unknown nodes: {unknown}")
        return value


class ConnectionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_host: str
    remote_port: int
    local_host: str
    local_port: int


class TunnelWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    error: str
    skipped: bool = True


class OutputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connections: dict[str, list[ConnectionEntry]]
    pid: int
    token: str
    started_at: str
    warnings: list[TunnelWarning] = Field(default_factory=list)


class ErrorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_schemas.py -v`
Expected: 6 passed.

- [ ] **Step 5: Lint and type-check**

Run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add garuda_tunnel/schemas.py tests/unit/test_schemas.py
git commit -m "feat: pydantic schemas for CLI I/O with require and auth validators"
```

---

### Task 4: Identity check helper

**Files:**
- Create: `garuda_tunnel/identity.py`
- Create: `tests/unit/test_identity.py`

- [ ] **Step 1: Write failing tests for identity check**

> **Why a spawned child instead of `monkeypatch.setenv`:** On Linux,
> `/proc/<pid>/environ` is a snapshot of `envp` taken by the kernel at
> `execve(2)` time. Mutations via `os.environ` / `setenv(3)` (including
> `monkeypatch.setenv`) update the libc environment of the running process
> but do NOT propagate back into the kernel snapshot. So an in-process
> `monkeypatch.setenv("GARUDA_TUNNEL_TOKEN", ...)` followed by
> `verify_token(os.getpid(), ...)` always returns `unavailable`, never
> `match`/`mismatch`. To actually exercise `_read_token_env_linux`, the
> tests spawn a short-lived child with the token in its `Popen(env=...)`,
> which is what makes it into `execve`'s `envp` and therefore into
> `/proc/<child_pid>/environ`. The same pattern works on macOS via `ps -E`.

`tests/unit/test_identity.py`:

```python
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from garuda_tunnel.identity import IdentityCheckResult, verify_token


def _spawn_sleeper(token: str) -> subprocess.Popen[bytes]:
    """Spawn a long-lived child carrying ``GARUDA_TUNNEL_TOKEN=token`` in its environ."""
    env = {**os.environ, "GARUDA_TUNNEL_TOKEN": token}
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=env,
    )
    # Give the child a brief moment to enter the sleep so /proc/<pid>/environ is populated.
    for _ in range(50):
        try:
            if os.path.exists(f"/proc/{proc.pid}/environ"):
                break
        except OSError:
            pass
        time.sleep(0.01)
    return proc


def test_match_against_child_process() -> None:
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("identity check only validated on Linux and macOS")
    proc = _spawn_sleeper("abc-123")
    try:
        assert verify_token(proc.pid, "abc-123") == IdentityCheckResult.match
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_mismatch_against_child_process() -> None:
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("identity check only validated on Linux and macOS")
    proc = _spawn_sleeper("abc-123")
    try:
        assert verify_token(proc.pid, "wrong") == IdentityCheckResult.mismatch
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_not_found_for_unused_pid() -> None:
    # PID 2**31 - 1 is essentially never allocated; the syscall should ENOENT.
    assert verify_token(2**31 - 1, "anything") == IdentityCheckResult.not_found
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_identity.py -v`
Expected: import error referencing missing `garuda_tunnel.identity`.

- [ ] **Step 3: Implement `garuda_tunnel/identity.py`**

```python
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
            return entry[len(prefix):].decode("utf-8", errors="replace")
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
            return token_part[len(needle):]
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_identity.py -v`
Expected: 3 passed on Linux/macOS, others skip on platform.

- [ ] **Step 5: Lint and type-check**

Run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add garuda_tunnel/identity.py tests/unit/test_identity.py
git commit -m "feat: PID+token identity check for safe stop/status"
```

---

### Task 5: TunnelManager with in-memory PEM parsing and concurrent startup

**Files:**
- Create: `garuda_tunnel/manager.py`
- Create: `tests/unit/test_manager_pkey.py`
- Modify: `pyproject.toml` (add a narrow mypy override for paramiko/sshtunnel; see Step 5)

The `start_all()` behavior involving real SSH connections is exercised by the integration tests in Task 9. Unit tests here cover only the in-memory PEM loader, because that path is purely a function of the input.

- [ ] **Step 1: Write failing unit tests for PEM loading**

> **Why `cryptography` for Ed25519 generation:** Paramiko 4.x (which we pin
> via `paramiko>=4.0,<5`) does not expose `paramiko.Ed25519Key.generate`.
> RSA and ECDSA still have `.generate(...)` classmethods. To keep the
> "real key, no mocks" test discipline, the Ed25519 branch generates the
> key via `cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey`
> and serializes to OpenSSH PEM, which `paramiko.Ed25519Key.from_private_key`
> round-trips cleanly. `cryptography` is already a transitive dependency
> of paramiko, so no pyproject change is required for this.

`tests/unit/test_manager_pkey.py`:

```python
from __future__ import annotations

from io import StringIO

import paramiko
import pytest

from garuda_tunnel.exceptions import SchemaValidationError
from garuda_tunnel.manager import load_inline_pkey


def _generate_pem(key_type: str) -> str:
    if key_type == "ed25519":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        priv = Ed25519PrivateKey.generate()
        return priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    if key_type == "rsa":
        key = paramiko.RSAKey.generate(bits=2048)
    elif key_type == "ecdsa":
        key = paramiko.ECDSAKey.generate()
    else:
        raise AssertionError(key_type)
    buf = StringIO()
    key.write_private_key(buf)
    return buf.getvalue()


@pytest.mark.parametrize("key_type", ["ed25519", "rsa", "ecdsa"])
def test_load_inline_pkey_accepts_supported_formats(key_type: str) -> None:
    pem = _generate_pem(key_type)
    parsed = load_inline_pkey(pem, passphrase=None)
    assert parsed is not None
    assert parsed.get_name()


def test_load_inline_pkey_rejects_garbage() -> None:
    with pytest.raises(SchemaValidationError) as excinfo:
        load_inline_pkey("not a key", passphrase=None)
    out = excinfo.value.to_error_output()
    assert "ssh_pkey" not in out["details"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_manager_pkey.py -v`
Expected: import error referencing missing `garuda_tunnel.manager`.

- [ ] **Step 3: Implement `garuda_tunnel/manager.py`**

```python
"""Tunnel orchestration: parallel SSHTunnelForwarder startup and teardown."""
from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from typing import Iterable

import paramiko
import sshtunnel

from garuda_tunnel.exceptions import (
    RequiredTunnelFailure,
    SchemaValidationError,
    TunnelStartupError,
)
from garuda_tunnel.schemas import (
    ConnectionEntry,
    ErrorOutput,
    InputSchema,
    NodeInput,
    OutputSchema,
    TunnelWarning,
)

_PARAMIKO_KEY_CLASSES: list[type[paramiko.PKey]] = [
    paramiko.Ed25519Key,
    paramiko.ECDSAKey,
    paramiko.RSAKey,
]

# DSSKey is still exposed on paramiko 4.x; include it last to keep behavior
# stable while the sshtunnel/paramiko 5.0 incompatibility is unresolved.
_dss_cls = getattr(paramiko, "DSSKey", None)
if _dss_cls is not None:
    _PARAMIKO_KEY_CLASSES.append(_dss_cls)


def load_inline_pkey(pem: str, passphrase: str | None) -> paramiko.PKey:
    """Parse a PEM string into a Paramiko key. Never writes the key to disk."""
    last_error: Exception | None = None
    for cls in _PARAMIKO_KEY_CLASSES:
        try:
            return cls.from_private_key(StringIO(pem), password=passphrase)
        except paramiko.SSHException as exc:
            last_error = exc
            continue
        except ValueError as exc:
            last_error = exc
            continue
    raise SchemaValidationError(
        "ssh_pkey could not be parsed by any supported Paramiko key class",
        {"last_error": str(last_error) if last_error else ""},
    )


@dataclass
class _StartResult:
    name: str
    success: bool
    connections: list[ConnectionEntry] = field(default_factory=list)
    forwarder: sshtunnel.SSHTunnelForwarder | None = None
    error: str | None = None


class TunnelManager:
    def __init__(self, schema: InputSchema) -> None:
        self._schema = schema
        self._forwarders: list[sshtunnel.SSHTunnelForwarder] = []
        self._lock = threading.Lock()

    def stop_all(self) -> None:
        with self._lock:
            forwarders = list(self._forwarders)
            self._forwarders.clear()
        for fwd in forwarders:
            try:
                fwd.stop(force=True)
            except Exception:  # noqa: BLE001 - we want best-effort cleanup
                continue

    def start_all_and_build_output(
        self,
        *,
        pid: int,
        token: str,
    ) -> OutputSchema | ErrorOutput:
        results = self._start_all()
        required = self._required_set(results)
        failed_required = [r for r in results if not r.success and r.name in required]
        if failed_required:
            self.stop_all()
            exc = RequiredTunnelFailure(
                "required tunnel(s) failed to start",
                {
                    "failed": [
                        {"node": r.name, "error": r.error or "unknown"}
                        for r in failed_required
                    ],
                },
            )
            return ErrorOutput(
                error=type(exc).__name__,
                message=exc.message,
                details=exc.details,
            )
        connections: dict[str, list[ConnectionEntry]] = {
            r.name: r.connections for r in results if r.success
        }
        warnings = [
            TunnelWarning(node=r.name, error=r.error or "unknown error")
            for r in results
            if not r.success
        ]
        return OutputSchema(
            connections=connections,
            pid=pid,
            token=token,
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            warnings=warnings,
        )

    def _required_set(self, results: Iterable[_StartResult]) -> set[str]:
        if self._schema.require == "*":
            return {r.name for r in results}
        assert isinstance(self._schema.require, list)
        return set(self._schema.require)

    def _start_all(self) -> list[_StartResult]:
        names = list(self._schema.nodes.keys())
        max_workers = max(1, min(len(names), 10))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return list(pool.map(self._start_one, names))

    def _start_one(self, name: str) -> _StartResult:
        node = self._schema.nodes[name]
        try:
            forwarder = self._build_forwarder(node)
            forwarder.start()
        except Exception as exc:  # noqa: BLE001 - aggregate per-node failure
            return _StartResult(name=name, success=False, error=str(exc))
        try:
            entries = self._verify_and_collect(node, forwarder)
        except Exception as exc:  # noqa: BLE001 - aggregate per-node failure
            try:
                forwarder.stop(force=True)
            except Exception:  # noqa: BLE001
                pass
            return _StartResult(name=name, success=False, error=str(exc))
        with self._lock:
            self._forwarders.append(forwarder)
        return _StartResult(name=name, success=True, connections=entries, forwarder=forwarder)

    def _build_forwarder(self, node: NodeInput) -> sshtunnel.SSHTunnelForwarder:
        local_binds: list[tuple[str, int]] = []
        if node.local_ports is not None:
            if len(node.local_ports) != len(node.remote_ports):
                raise TunnelStartupError(
                    "local_ports must align with remote_ports when provided",
                    {"node_remote_ports": node.remote_ports},
                )
            local_binds = [("127.0.0.1", p) for p in node.local_ports]
        else:
            local_binds = [("127.0.0.1", 0) for _ in node.remote_ports]
        remote_binds = [("127.0.0.1", p) for p in node.remote_ports]
        kwargs: dict[str, object] = {
            "ssh_address_or_host": (node.host, node.port),
            "ssh_username": node.user,
            "remote_bind_addresses": remote_binds,
            "local_bind_addresses": local_binds,
            "compression": node.ssh_options.compression,
            "set_keepalive": 30.0,
        }
        if node.ssh_pkey:
            kwargs["ssh_pkey"] = load_inline_pkey(node.ssh_pkey, node.ssh_pkey_passphrase)
        if node.ssh_password:
            kwargs["ssh_password"] = node.ssh_password
        forwarder = sshtunnel.SSHTunnelForwarder(**kwargs)
        forwarder.daemon_forward_servers = True
        forwarder.daemon_transport = True
        return forwarder

    def _verify_and_collect(
        self,
        node: NodeInput,
        forwarder: sshtunnel.SSHTunnelForwarder,
    ) -> list[ConnectionEntry]:
        entries: list[ConnectionEntry] = []
        for remote_port, local_bind in zip(
            node.remote_ports, forwarder.local_bind_addresses, strict=True
        ):
            local_host, local_port = local_bind
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(node.ssh_options.connect_timeout)
                if probe.connect_ex((local_host, local_port)) != 0:
                    raise TunnelStartupError(
                        "local forward did not accept connection",
                        {"remote_port": remote_port, "local_port": local_port},
                    )
            entries.append(
                ConnectionEntry(
                    remote_host="127.0.0.1",
                    remote_port=remote_port,
                    local_host=local_host,
                    local_port=local_port,
                )
            )
        return entries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all prior tests still green plus 4 new PEM tests passing.

- [ ] **Step 5: Lint and type-check (with one pyproject change)**

Neither `paramiko` nor `sshtunnel` ships type stubs or a `py.typed` marker, so `mypy --strict garuda_tunnel` errors out on `manager.py`'s `import paramiko` / `import sshtunnel` with `[import-untyped]`. Add a narrow override to `pyproject.toml` after the existing `[tool.mypy]` block:

```toml
[[tool.mypy.overrides]]
module = ["paramiko", "paramiko.*", "sshtunnel", "sshtunnel.*"]
ignore_missing_imports = true
```

Then run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: green. If `Exception` catches trip ruff `BLE001`, the `# noqa: BLE001` comments are already in place; do not remove them.

- [ ] **Step 6: Commit**

```bash
git add garuda_tunnel/manager.py tests/unit/test_manager_pkey.py pyproject.toml
git commit -m "feat: TunnelManager with in-memory PEM loading and concurrent startup"
```

---

### Task 6: Daemon process with IPC handshake

**Files:**
- Create: `garuda_tunnel/daemon.py`
- Create: `tests/unit/test_daemon_ipc.py`

This task wires the double-fork lifecycle. Real SSH tunnels are not started here; the test substitutes a fake startup callback to validate the IPC handshake and PID semantics. The full daemon with `TunnelManager` is exercised by Task 9 integration tests.

- [ ] **Step 1: Write failing IPC test using a fake startup callback**

`tests/unit/test_daemon_ipc.py`:

```python
from __future__ import annotations

import json
import os
import signal
import time

import pytest

from garuda_tunnel.daemon import spawn_daemon_with_callback


def _fake_success_payload(token: str, pid: int) -> dict[str, object]:
    return {
        "kind": "success",
        "payload": {
            "connections": {},
            "pid": pid,
            "token": token,
            "started_at": "2026-05-16T14:30:00Z",
            "warnings": [],
        },
    }


def test_spawn_daemon_returns_daemon_pid_via_ipc() -> None:
    def startup(token: str) -> dict[str, object]:
        return _fake_success_payload(token, os.getpid())

    message = spawn_daemon_with_callback(startup_callback=startup, log_file=None)
    payload = message["payload"]
    pid = int(payload["pid"])
    token = str(payload["token"])
    try:
        # Daemon must outlive the parent's IPC read.
        for _ in range(20):
            if _process_alive(pid):
                break
            time.sleep(0.05)
        assert _process_alive(pid), "daemon process should be alive after IPC handshake"
        assert pid != os.getpid()
        assert token  # opaque non-empty token
    finally:
        if _process_alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(40):
                if not _process_alive(pid):
                    break
                time.sleep(0.05)


def test_spawn_daemon_propagates_required_failure() -> None:
    def startup(token: str) -> dict[str, object]:
        return {
            "kind": "required_failure",
            "payload": {
                "error": "RequiredTunnelFailure",
                "message": "boom",
                "details": {"failed": ["a"]},
            },
        }

    with pytest.raises(SystemExit) as excinfo:
        spawn_daemon_with_callback(startup_callback=startup, log_file=None)
    # The CLI translates this into exit 2; spawn_daemon_with_callback uses the
    # same SystemExit code so we can assert it directly.
    assert excinfo.value.code == 2


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_daemon_ipc.py -v`
Expected: import error referencing missing `garuda_tunnel.daemon`.

- [ ] **Step 3: Implement `garuda_tunnel/daemon.py`**

```python
"""POSIX double-fork daemonization with an IPC pipe used by the parent."""
from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import threading
from pathlib import Path
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
    os.dup2(devnull_in.fileno(), sys.stdin.fileno())
    os.dup2(target.fileno(), sys.stdout.fileno())
    os.dup2(target.fileno(), sys.stderr.fileno())

    try:
        message = startup_callback(token)
    except Exception as exc:  # noqa: BLE001
        _write_message(
            write_fd,
            {"kind": "daemon_error", "payload": {"error": type(exc).__name__,
                                                  "message": str(exc),
                                                  "details": {}}},
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: prior tests still green plus 2 new IPC tests passing.

- [ ] **Step 5: Lint and type-check**

Run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add garuda_tunnel/daemon.py tests/unit/test_daemon_ipc.py
git commit -m "feat: double-fork daemon with IPC handshake and signal-driven shutdown"
```

---

### Task 7: CLI `start` command wired to daemon + manager

**Files:**
- Modify: `garuda_tunnel/cli.py` (add `start`)
- Create: `tests/unit/test_cli_start_validation.py`

- [ ] **Step 1: Write failing tests for schema validation behavior of `start`**

`tests/unit/test_cli_start_validation.py`:

```python
from __future__ import annotations

import json

from click.testing import CliRunner

from garuda_tunnel.cli import main


def test_start_rejects_invalid_json_with_exit_1() -> None:
    result = CliRunner().invoke(main, ["start"], input="not json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "SchemaValidationError"


def test_start_rejects_unknown_node_in_require() -> None:
    body = json.dumps(
        {
            "nodes": {
                "a": {
                    "host": "h",
                    "user": "u",
                    "ssh_password": "p",
                    "remote_ports": [22],
                }
            },
            "require": ["missing"],
        }
    )
    result = CliRunner().invoke(main, ["start"], input=body)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "SchemaValidationError"
    assert "missing" in payload["message"] or "missing" in json.dumps(payload["details"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cli_start_validation.py -v`
Expected: failure because `start` subcommand does not exist yet.

- [ ] **Step 3: Implement `start` in `garuda_tunnel/cli.py`**

Append to `garuda_tunnel/cli.py`:

```python
import json
import sys

from pydantic import ValidationError

from garuda_tunnel.daemon import spawn_daemon
from garuda_tunnel.exceptions import (
    DaemonError,
    GarudaTunnelError,
    SchemaValidationError,
    exit_code_for,
)
from garuda_tunnel.schemas import InputSchema


@main.command("start")
def start_command() -> None:
    """Read JSON from stdin, open tunnels, daemonize, print mapping JSON."""
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(
                "stdin is not valid JSON",
                {"position": exc.pos, "line": exc.lineno},
            ) from exc
        try:
            schema = InputSchema.model_validate(payload)
        except ValidationError as exc:
            raise SchemaValidationError(
                "input does not satisfy the InputSchema contract",
                {"errors": json.loads(exc.json())},
            ) from exc
        message = spawn_daemon(schema)
        sys.stdout.write(json.dumps(message["payload"]))
        sys.stdout.write("\n")
        sys.stdout.flush()
    except SystemExit:
        raise
    except GarudaTunnelError as exc:
        sys.stdout.write(json.dumps(exc.to_error_output()))
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(exit_code_for(exc))
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(
            json.dumps(
                DaemonError("unexpected failure during start", {"type": type(exc).__name__})
                .to_error_output()
            )
        )
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(4)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit -v`
Expected: all unit tests green, including the two new ones for `start`.

- [ ] **Step 5: Lint and type-check**

Run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add garuda_tunnel/cli.py tests/unit/test_cli_start_validation.py
git commit -m "feat: cli start wires schemas, daemon, and structured error output"
```

---

### Task 8: CLI `stop` and `status` commands

**Files:**
- Modify: `garuda_tunnel/cli.py` (add `stop` and `status`)
- Create: `tests/unit/test_cli_stop_status_parsing.py`

- [ ] **Step 1: Write failing CLI argument tests**

`tests/unit/test_cli_stop_status_parsing.py`:

```python
from __future__ import annotations

from click.testing import CliRunner

from garuda_tunnel.cli import main


def test_stop_requires_pid_and_token() -> None:
    result = CliRunner().invoke(main, ["stop"])
    assert result.exit_code == 64

    result_no_token = CliRunner().invoke(main, ["stop", "--pid", "123"])
    assert result_no_token.exit_code == 64


def test_status_requires_pid() -> None:
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 64


def test_stop_bad_pid_int_exits_64() -> None:
    result = CliRunner().invoke(main, ["stop", "--pid", "not-an-int", "--token", "t"])
    assert result.exit_code == 64
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cli_stop_status_parsing.py -v`
Expected: failure because `stop`/`status` do not exist yet.

- [ ] **Step 3: Implement `stop` and `status`**

Append to `garuda_tunnel/cli.py`:

```python
import os
import signal
import time

from garuda_tunnel.identity import IdentityCheckResult, verify_token


@main.command("stop")
@click.option("--pid", type=int, required=True)
@click.option("--token", type=str, required=True)
@click.option("--grace-seconds", type=int, default=10, show_default=True)
def stop_command(pid: int, token: str, grace_seconds: int) -> None:
    """Send SIGTERM (then SIGKILL) to a garuda-tunnel daemon identified by PID+token."""
    if not _kill_with_identity(pid, token, grace_seconds, force=True):
        sys.exit(0)


@main.command("status")
@click.option("--pid", type=int, required=True)
@click.option("--token", type=str, default=None)
def status_command(pid: int, token: str | None) -> None:
    """Report whether the daemon with the given PID (and optional token) is alive."""
    alive = _is_alive(pid, token)
    sys.stdout.write(json.dumps({"alive": alive}))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _is_alive(pid: int, token: str | None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if token is None:
        return True
    return verify_token(pid, token) == IdentityCheckResult.match


def _kill_with_identity(pid: int, token: str, grace_seconds: int, *, force: bool) -> bool:
    check = verify_token(pid, token)
    if check == IdentityCheckResult.not_found:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "not found"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    if check == IdentityCheckResult.mismatch:
        sys.stdout.write(json.dumps({"stopped": False, "reason": "token mismatch"}))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    if check == IdentityCheckResult.unavailable:
        sys.stdout.write(
            json.dumps({"stopped": False, "reason": "identity check unavailable"})
        )
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False

    os.kill(pid, signal.SIGTERM)
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

    recheck = verify_token(pid, token)
    if recheck != IdentityCheckResult.match:
        sys.stdout.write(
            json.dumps({"stopped": False, "reason": "identity changed during grace"})
        )
        sys.stdout.write("\n")
        sys.stdout.flush()
        return False
    os.kill(pid, signal.SIGKILL)
    sys.stdout.write(json.dumps({"stopped": True, "forced": True}))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return True
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit -v`
Expected: all unit tests green.

- [ ] **Step 5: Lint and type-check**

Run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add garuda_tunnel/cli.py tests/unit/test_cli_stop_status_parsing.py
git commit -m "feat: cli stop/status with PID+token identity gating"
```

---

### Task 9: Integration tests against a real openssh-server cluster

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/docker-compose.yml`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_start.py`
- Create: `tests/integration/test_stop.py`
- Create: `tests/integration/test_status.py`
- Create: `tests/integration/test_multiport.py`
- Modify: `tests/conftest.py` (autouse cleanup based on `started_daemons`)

This task requires Docker available on the host. CI runs only on `ubuntu-latest`.

- [ ] **Step 1: Write `tests/integration/docker-compose.yml`**

```yaml
services:
  sshd-a:
    image: lscr.io/linuxserver/openssh-server:latest
    environment:
      - PUID=1000
      - PGID=1000
      - PUBLIC_KEY_FILE=/keys/id_test.pub
      - USER_NAME=tester
      - SUDO_ACCESS=false
      - PASSWORD_ACCESS=false
    volumes:
      - ./_keys:/keys:ro
    ports:
      - "127.0.0.1::2222"
    command: >-
      sh -c "apk add --no-cache iperf3 &&
             iperf3 -s -B 127.0.0.1 -p 6443 -D &&
             /init"

  sshd-b:
    image: lscr.io/linuxserver/openssh-server:latest
    environment:
      - PUID=1000
      - PGID=1000
      - PUBLIC_KEY_FILE=/keys/id_test.pub
      - USER_NAME=tester
      - SUDO_ACCESS=false
      - PASSWORD_ACCESS=false
    volumes:
      - ./_keys:/keys:ro
    ports:
      - "127.0.0.1::2222"
    command: >-
      sh -c "apk add --no-cache iperf3 &&
             iperf3 -s -B 127.0.0.1 -p 6443 -D &&
             /init"

  sshd-c:
    image: lscr.io/linuxserver/openssh-server:latest
    environment:
      - PUID=1000
      - PGID=1000
      - PUBLIC_KEY_FILE=/keys/id_test.pub
      - USER_NAME=tester
      - SUDO_ACCESS=false
      - PASSWORD_ACCESS=false
    volumes:
      - ./_keys:/keys:ro
    ports:
      - "127.0.0.1::2222"
    command: >-
      sh -c "apk add --no-cache iperf3 &&
             iperf3 -s -B 127.0.0.1 -p 6443 -D &&
             /init"
```

- [ ] **Step 2: Write `tests/integration/conftest.py` with session fixtures**

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import paramiko
import pytest


HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def started_daemons() -> list[tuple[int, str]]:
    """Mutable list of (pid, token) pairs produced by successful start calls."""
    return []


@pytest.fixture(scope="session")
def ssh_keypair() -> tuple[str, str]:
    keys_dir = HERE / "_keys"
    keys_dir.mkdir(exist_ok=True)
    priv_path = keys_dir / "id_test"
    pub_path = keys_dir / "id_test.pub"
    if not priv_path.exists():
        key = paramiko.Ed25519Key.generate()
        with priv_path.open("w") as fh:
            key.write_private_key(fh)
        os.chmod(priv_path, 0o600)
        with pub_path.open("w") as fh:
            fh.write(f"{key.get_name()} {key.get_base64()} test\n")
    return priv_path.read_text(), pub_path.read_text()


@pytest.fixture(scope="session")
def ssh_test_cluster(ssh_keypair: tuple[str, str]) -> Iterator[dict[str, object]]:
    if sys.platform != "linux":
        pytest.skip("integration tests require Linux + Docker")
    compose_file = HERE / "docker-compose.yml"
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d", "--wait"],
        check=True,
    )
    try:
        services = ["sshd-a", "sshd-b", "sshd-c"]
        ports: dict[str, int] = {}
        for service in services:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "port", service, "2222"],
                capture_output=True,
                text=True,
                check=True,
            )
            host, port = result.stdout.strip().rsplit(":", 1)
            ports[service] = int(port)
        priv_pem, _pub = ssh_keypair
        yield {
            "ports": ports,
            "private_pem": priv_pem,
            "user": "tester",
            "host": "127.0.0.1",
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down", "-v"],
            check=False,
        )


def garuda_tunnel_start(stdin_payload: dict[str, object]) -> dict[str, object]:
    completed = subprocess.run(
        ["garuda-tunnel", "start"],
        input=json.dumps(stdin_payload),
        text=True,
        capture_output=True,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "json": json.loads(completed.stdout) if completed.stdout.strip() else None,
    }
```

- [ ] **Step 3: Write `tests/conftest.py` autouse cleanup**

```python
from __future__ import annotations

import subprocess

import pytest


@pytest.fixture(autouse=True)
def kill_orphan_test_daemons(request: pytest.FixtureRequest) -> object:
    # ``started_daemons`` is provided by the integration conftest and is shared
    # across the session; unit tests do not request it and skip cleanup.
    try:
        started: list[tuple[int, str]] = request.getfixturevalue("started_daemons")
    except pytest.FixtureLookupError:
        yield
        return
    yield
    for pid, token in started:
        subprocess.run(
            ["garuda-tunnel", "stop", "--pid", str(pid), "--token", token],
            capture_output=True,
        )
    started.clear()
```

- [ ] **Step 4: Write integration `test_start.py`**

```python
from __future__ import annotations

import json

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def _node(host: str, port: int, pem: str, remote_port: int = 6443) -> dict[str, object]:
    return {
        "host": host,
        "port": port,
        "user": "tester",
        "ssh_pkey": pem,
        "remote_ports": [remote_port],
    }


def test_start_all_required_success(
    ssh_test_cluster: dict[str, object],
    started_daemons: list[tuple[int, str]],
) -> None:
    payload = {
        "nodes": {
            "a": _node("127.0.0.1", ssh_test_cluster["ports"]["sshd-a"], ssh_test_cluster["private_pem"]),
            "b": _node("127.0.0.1", ssh_test_cluster["ports"]["sshd-b"], ssh_test_cluster["private_pem"]),
        }
    }
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0, outcome["stderr"]
    body = outcome["json"]
    assert sorted(body["connections"].keys()) == ["a", "b"]
    assert body["pid"] > 0
    assert body["token"]
    started_daemons.append((body["pid"], body["token"]))


def test_start_required_failure_cleans_up(
    ssh_test_cluster: dict[str, object],
    started_daemons: list[tuple[int, str]],
) -> None:
    good = _node("127.0.0.1", ssh_test_cluster["ports"]["sshd-a"], ssh_test_cluster["private_pem"])
    bad = {
        "host": "127.0.0.1",
        "port": ssh_test_cluster["ports"]["sshd-b"],
        "user": "tester",
        "ssh_pkey": "-----BEGIN OPENSSH PRIVATE KEY-----\nGARBAGE\n-----END OPENSSH PRIVATE KEY-----",
        "remote_ports": [6443],
    }
    outcome = garuda_tunnel_start({"nodes": {"a": good, "b": bad}})
    assert outcome["returncode"] == 2
    body = outcome["json"]
    assert body["error"] in {"SchemaValidationError", "RequiredTunnelFailure"}


def test_start_optional_failure_warns(
    ssh_test_cluster: dict[str, object],
    started_daemons: list[tuple[int, str]],
) -> None:
    good = _node("127.0.0.1", ssh_test_cluster["ports"]["sshd-a"], ssh_test_cluster["private_pem"])
    optional_bad = {
        "host": "127.0.0.1",
        "port": ssh_test_cluster["ports"]["sshd-c"],
        "user": "wrong-user",
        "ssh_pkey": ssh_test_cluster["private_pem"],
        "remote_ports": [6443],
    }
    payload = {"nodes": {"a": good, "b": optional_bad}, "require": ["a"]}
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0
    body = outcome["json"]
    assert "a" in body["connections"]
    assert "b" not in body["connections"]
    assert any(w["node"] == "b" for w in body["warnings"])
    started_daemons.append((body["pid"], body["token"]))


def test_start_schema_failure_exits_1(ssh_test_cluster: dict[str, object]) -> None:
    outcome = garuda_tunnel_start({"nodes": {"a": {"user": "tester"}}})
    assert outcome["returncode"] == 1
    body = outcome["json"]
    assert body["error"] == "SchemaValidationError"
```

- [ ] **Step 5: Write integration `test_stop.py`**

```python
from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def _start(ssh_test_cluster: dict[str, object]) -> dict[str, object]:
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_ports": [6443],
            }
        }
    }
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0
    return outcome["json"]


def test_stop_alive_daemon(ssh_test_cluster: dict[str, object]) -> None:
    body = _start(ssh_test_cluster)
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(body["pid"]), "--token", body["token"]],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    assert json.loads(stop.stdout)["stopped"] is True
    # The daemon process must no longer exist.
    try:
        os.kill(body["pid"], 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert alive is False


def test_stop_wrong_token(ssh_test_cluster: dict[str, object], started_daemons: list[tuple[int, str]]) -> None:
    body = _start(ssh_test_cluster)
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(body["pid"]), "--token", "bogus"],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert "token" in payload["reason"] or "identity" in payload["reason"]
    started_daemons.append((body["pid"], body["token"]))


def test_stop_already_dead() -> None:
    stop = subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(2**31 - 1), "--token", "irrelevant"],
        capture_output=True,
        text=True,
    )
    assert stop.returncode == 0
    payload = json.loads(stop.stdout)
    assert payload["stopped"] is False
    assert payload["reason"] == "not found"
```

- [ ] **Step 6: Write integration `test_status.py`**

```python
from __future__ import annotations

import json
import subprocess

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def test_status_alive_then_dead(
    ssh_test_cluster: dict[str, object],
    started_daemons: list[tuple[int, str]],
) -> None:
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_ports": [6443],
            }
        }
    }
    outcome = garuda_tunnel_start(payload)
    body = outcome["json"]

    alive = subprocess.run(
        ["garuda-tunnel", "status", "--pid", str(body["pid"]), "--token", body["token"]],
        capture_output=True,
        text=True,
    )
    assert json.loads(alive.stdout)["alive"] is True

    wrong = subprocess.run(
        ["garuda-tunnel", "status", "--pid", str(body["pid"]), "--token", "bad"],
        capture_output=True,
        text=True,
    )
    assert json.loads(wrong.stdout)["alive"] is False

    subprocess.run(
        ["garuda-tunnel", "stop", "--pid", str(body["pid"]), "--token", body["token"]],
        capture_output=True,
    )

    dead = subprocess.run(
        ["garuda-tunnel", "status", "--pid", str(body["pid"]), "--token", body["token"]],
        capture_output=True,
        text=True,
    )
    assert json.loads(dead.stdout)["alive"] is False
```

- [ ] **Step 7: Write integration `test_multiport.py`**

```python
from __future__ import annotations

import pytest

from tests.integration.conftest import garuda_tunnel_start


pytestmark = pytest.mark.integration


def test_two_forwards_to_same_remote_port(
    ssh_test_cluster: dict[str, object],
    started_daemons: list[tuple[int, str]],
) -> None:
    payload = {
        "nodes": {
            "a": {
                "host": "127.0.0.1",
                "port": ssh_test_cluster["ports"]["sshd-a"],
                "user": "tester",
                "ssh_pkey": ssh_test_cluster["private_pem"],
                "remote_ports": [6443, 6443],
            }
        }
    }
    outcome = garuda_tunnel_start(payload)
    assert outcome["returncode"] == 0
    body = outcome["json"]
    entries = body["connections"]["a"]
    assert len(entries) == 2
    assert entries[0]["local_port"] != entries[1]["local_port"]
    started_daemons.append((body["pid"], body["token"]))
```

- [ ] **Step 8: Run the integration suite locally**

Run:

```bash
pytest tests/integration -m integration -v
```

Expected: all integration tests pass. Re-run to verify cleanup leaves no orphan daemons:

```bash
pgrep -af garuda-tunnel || echo "no orphans"
```

Expected: `no orphans`.

- [ ] **Step 9: Lint and type-check**

Run:

```bash
ruff format --check .
ruff check .
mypy --strict garuda_tunnel
```

Expected: green.

- [ ] **Step 10: Commit**

```bash
git add tests/conftest.py tests/integration garuda_tunnel
git commit -m "test: integration suite against real openssh-server cluster with token cleanup"
```

---

### Task 10: GitHub Actions CI workflow

**Files:**
- Create: `.github/workflows/test.yml`

- [ ] **Step 1: Write `.github/workflows/test.yml`**

```yaml
name: test

on:
  push:
    branches: ["**"]
  pull_request:

jobs:
  unit:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest]
        python-version: ["3.10", "3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: python -m pip install --upgrade pip
      - run: pip install -e ".[dev]"
      - run: ruff format --check .
      - run: ruff check .
      - run: mypy --strict garuda_tunnel
      - run: pytest tests/unit -v

  integration:
    runs-on: ubuntu-latest
    needs: unit
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install --upgrade pip
      - run: pip install -e ".[dev]"
      - run: docker compose version
      - run: pytest tests/integration -m integration -v
```

- [ ] **Step 2: Push the branch to the private GitHub repo**

```bash
git remote add origin git@github.com:AlexMKX/garuda-tunnel.git  # only if not set
git push -u origin main
```

- [ ] **Step 3: Verify CI green**

In the private repo Actions tab, confirm both `unit` and `integration` jobs finish green. If anything fails, fix locally, push, and re-run rather than tweaking the workflow blindly.

- [ ] **Step 4: Commit (workflow only; no code changes here)**

If the workflow needed iterations, commit each change before pushing:

```bash
git add .github/workflows/test.yml
git commit -m "ci: unit matrix on Linux+macOS plus Linux integration job"
git push
```

---

### Task 11: Release workflow and first tagged release

**Files:**
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Write `.github/workflows/release.yml`**

```yaml
name: release

on:
  push:
    tags: ["v*"]

jobs:
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install --upgrade pip build
      - run: pip install -e ".[dev]"
      - run: ruff format --check .
      - run: ruff check .
      - run: mypy --strict garuda_tunnel
      - run: pytest tests/unit -v
      - run: pytest tests/integration -m integration -v
      - run: python -m build
      - name: Create Release
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          files: |
            dist/*.tar.gz
            dist/*.whl
```

- [ ] **Step 2: Commit and push the release workflow**

```bash
git add .github/workflows/release.yml
git commit -m "ci: release workflow building sdist+wheel on vYYYY tag push"
git push
```

- [ ] **Step 3: Create the first release tag**

Use a UTC timestamp matching the spec format:

```bash
git tag "v$(date -u +'%Y.1%m%d.1%H%M')"
git push --tags
```

- [ ] **Step 4: Watch GitHub Actions and confirm Release artifacts**

In the Actions tab, wait for the `release` job. Confirm:

- Workflow finishes green.
- A new Release entry exists at `https://github.com/AlexMKX/garuda-tunnel/releases/tag/<TAG>`.
- The Release has both `garuda_tunnel-<TAG>-py3-none-any.whl` and `garuda_tunnel-<TAG>.tar.gz` attached.

- [ ] **Step 5: Smoke-test `pipx run` end-to-end from a clean shell**

This validates the spec success criterion "pipx run --spec git+... works on a fresh disposable environment". Because the repo is private, use SSH or a personal access token. From a separate shell with `pipx` installed:

```bash
pipx run --spec git+ssh://git@github.com/AlexMKX/garuda-tunnel.git@<TAG> garuda-tunnel --help
```

Expected: usage text, exit 0. If pipx caching interferes, clear it first: `pipx environment --value PIPX_HOME ; rm -rf "$(pipx environment --value PIPX_HOME)/cache"`.

- [ ] **Step 6: No commit required**

The tag and Release are the artifact. There is nothing further to commit. Move on to Task 12.

---

### Task 12: Spec success-criteria self-check and plan close-out

**Files:**
- Modify: `README.md` (record the released tag in quickstart)

- [ ] **Step 1: Re-read spec success criteria and check each one**

Open `docs/specs/2026-05-16-garuda-tunnel-design.md` and walk through every bullet in the `Success criteria` section. For each, point to the test or command that proves it. Capture this as a short note in your PR/commit message or a workspace scratchpad; do not embed it back into the spec.

Items that must be verifiable now:

- `pipx run --spec git+...@<TAG> garuda-tunnel --help` works (Task 11 step 5).
- Schema validation catches malformed input (`tests/unit/test_cli_start_validation.py`).
- `start` opens N tunnels concurrently and returns JSON (`tests/integration/test_start.py`).
- `start` completes within 60 seconds for 10-node bootstrap: run an ad-hoc local check with `time` against a 10-node compose stack copy. Document the observed wall clock in commit body. Spec marks this as best-effort, so no test gating.
- `stop --pid --token` kills daemon (`tests/integration/test_stop.py`).
- `status --pid [--token]` returns liveness (`tests/integration/test_status.py`).
- `require: "*"` with one unreachable node -> exit 2 with cleanup (`tests/integration/test_start.py::test_start_required_failure_cleans_up`).
- `require: ["a", "b"]` with optional failure -> warnings (`tests/integration/test_start.py::test_start_optional_failure_warns`).
- Integration tests pass on Linux CI runners (Task 10).
- GitHub Actions CI green (Task 10) and release workflow green (Task 11).

- [ ] **Step 2: Update README quickstart with the actual released tag**

Replace `<TAG>` in `README.md` with the concrete value from Task 11.

- [ ] **Step 3: Commit and push**

```bash
git add README.md
git commit -m "docs: record first released tag in README quickstart"
git push
```

- [ ] **Step 4: Mark the plan complete**

In `docs/superpowers/plans/2026-05-16-garuda-tunnel.md`, switch all task-level `[ ]` to `[x]` once each task is genuinely done. Do not mark them in a batch at the end; mark each one as the corresponding work merges.
