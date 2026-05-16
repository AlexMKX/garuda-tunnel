# garuda-tunnel — design

**Status**: design
**Date**: 2026-05-16
**Repository**: https://github.com/AlexMKX/garuda-tunnel
**License**: MIT

---

## Context

Garuda's k3s edge pilot (see garuda-repo G1b/G2/G3 future specs) needs API
access from a disposable execution environment (CI runner, local container,
operator workstation). The constraints:

- k3s apiserver binds `127.0.0.1:6443` only on each edge node — no public
  apiserver exposure.
- The execution environment has no persistent state, no preinstalled SSH
  tunnels, no manual setup steps.
- The execution environment has SSH access to each edge node directly
  (whitelisted via firewall to operator IPs).
- `helm` / `kubectl` providers used from Terraform/OpenTofu need an
  apiserver endpoint that resolves at plan/apply time.

`garuda-tunnel` is a standalone Python CLI that opens SSH local-forward
tunnels to multiple nodes in one operation, returns the resulting
`127.0.0.1:port` mapping as JSON, and daemonizes. The caller (a Terragrunt
`before_hook`, a CI job script, an operator) saves the JSON, runs whatever
needs the tunnels, then kills the daemon by PID.

This tool is consumed by — but not coupled to — garuda's k3s workflow.
It is generic enough for any "open N SSH tunnels, get N local ports"
scenario.

## Goals

1. **Self-contained CLI** for opening N SSH local-forward tunnels in one
   invocation, returning port mapping for downstream consumers.

2. **DE-first**: runs via `pipx run --spec git+...` from an ephemeral
   environment with no persistent install. Reads input from stdin (JSON),
   writes output to stdout (JSON). No secret files on disk.

3. **Generic protocol**: no awareness of kubernetes, kubeconfig, kubectl.
   k3s API access is the first consumer, not the only one. Tunnel any
   remote-port-on-127.0.0.1 to a local port.

4. **Full `pahaz/sshtunnel` feature exposure**: multi-port forwards in one
   SSH session, key or password auth, SSH options (compression, host key
   policies, timeouts), threaded mode.

5. **Orchestrator-friendly lifecycle**: `start` daemonizes and returns
   metadata immediately; `stop --pid` cleanly tears down; `status --pid`
   reports liveness. Suitable for Terragrunt `before_hook`/`after_hook`
   wrapping or any external supervisor with PID-based control.

6. **Schema-validated I/O**: pydantic models for input/output, validation
   errors return structured JSON to stdout with non-zero exit. No "garbage
   in → confusing fault" semantics.

7. **Differentiated required vs optional tunnels**: caller specifies
   `require: "*"` (all required, default) or `require: [<names>]` (subset
   required, others best-effort). Required failures abort with cleanup;
   optional failures degrade gracefully with warnings.

## Non-goals

- NOT a k8s-specific tool. k3s API access is the primary consumer but the
  CLI does not know about kubeconfig, kubectl, helm, etc.
- NOT managing SSH host key discovery — `AutoAddPolicy` default
  (firewall-protected access is the trust boundary).
- NOT providing a stable Python library API for embedding. The CLI is the
  only stable contract. Python internals are implementation detail and may
  break between versions.
- NOT maintaining state between invocations (no state files, no caches).
  Each `start` is fresh; each `stop` operates on the PID provided.
- NOT Windows-first. Linux/macOS targeted. Windows may work via WSL,
  untested.
- NOT publishing to PyPI initially. Git-installable via `pipx run --spec`.
- NOT providing GUI/TUI. Pure CLI.

## Success criteria

- `pipx run --spec git+https://github.com/AlexMKX/garuda-tunnel.git@<TAG> garuda-tunnel --help`
  works on a fresh disposable environment.
- Schema validation catches malformed input; non-zero exit with structured
  JSON error.
- `start` opens N tunnels concurrently and returns JSON containing N
  entries in `connections` plus `pid`.
- `start` completes within 60 seconds for 10-node bootstrap (best-effort
  expectation, not enforced by tests).
- `stop --pid N` kills daemon, all tunnels cleaned, exit 0.
- `status --pid N` returns `{alive: true/false}`.
- `require: "*"` with one unreachable node → exit 2, all started tunnels
  rolled back.
- `require: ["a", "b"]` with `b` reachable, `c` (optional) unreachable →
  exit 0, `warnings` lists `c`.
- Integration tests pass on Linux CI runners against a containerized
  openssh sidecar.
- GitHub Actions CI green: lint, type-check, unit + integration tests.

## Out of scope

- Production hardening (audit logging, structured logging, metrics).
- Alternative SSH backends (ssh CLI, mosh).
- TLS/mTLS termination.
- IPv6 testing (should work, not validated).

---

## Architecture

### Package layout

```
garuda-tunnel/                          (separate git repo)
├── pyproject.toml                      Hatch-based, dynamic version from git tag
├── README.md                           Quickstart, schemas, examples
├── LICENSE                             MIT
├── .gitignore
├── .github/workflows/
│   ├── test.yml                        Unit matrix + integration on Linux
│   └── release.yml                     On tag push: build + GitHub Release
├── docs/specs/
│   └── 2026-05-16-garuda-tunnel-design.md     (this file)
├── garuda_tunnel/
│   ├── __init__.py                     `__version__` via importlib.metadata
│   ├── __main__.py                     `python -m garuda_tunnel` entrypoint
│   ├── cli.py                          Click commands: start, stop, status
│   ├── schemas.py                      Pydantic models for I/O
│   ├── manager.py                      TunnelManager: orchestration
│   ├── daemon.py                       Double-fork, signal handling
│   └── exceptions.py                   Typed exception hierarchy
└── tests/
    ├── conftest.py                     Autouse orphan cleanup
    ├── unit/
    │   ├── test_schemas.py             Pydantic validation
    │   └── test_cli_parsing.py         CliRunner: argument parsing only
    └── integration/
        ├── conftest.py                 `ssh_test_cluster` fixture (docker compose)
        ├── docker-compose.yml          openssh-server + iperf3 sidecars
        ├── test_start.py               Success, required failure, optional failure
        ├── test_stop.py                Graceful, force, idempotent
        ├── test_status.py              Alive, dead
        └── test_multiport.py           Multi-port per node
```

### Component responsibilities

| Component | Responsibility |
|---|---|
| `cli.py` | Click commands; read stdin / write stdout JSON; map exceptions to exit codes. |
| `schemas.py` | Pydantic models: `NodeInput`, `SSHOptions`, `DaemonOptions`, `InputSchema`, `ConnectionEntry`, `TunnelWarning`, `OutputSchema`, `ErrorOutput`. |
| `manager.py` | `TunnelManager`: holds list of `sshtunnel.SSHTunnelForwarder` instances; `start_all()` opens concurrently via `ThreadPoolExecutor`; aggregates per-node `StartResult`; `stop_all()` for cleanup. |
| `daemon.py` | `daemonize(log_file)`: POSIX double-fork, redirect stdin/stdout/stderr; `install_signal_handlers()` for SIGTERM/SIGINT; `wait_for_signal()` blocks on `signal.pause()`. |
| `exceptions.py` | Hierarchy: `GarudaTunnelError` → `SchemaValidationError`, `TunnelStartupError`, `RequiredTunnelFailure`, `DaemonError`. Each maps to a specific exit code. |
| `__main__.py` | `python -m garuda_tunnel` → `cli.main()`. |
| `__init__.py` | Exports `__version__` from `importlib.metadata`. No public Python API. |

### Data flow: `start`

```
caller                                     daemon process
------                                     --------------
1. echo $INPUT | garuda-tunnel start
2.   cli.start():
       parse stdin → dict
       schemas.InputSchema.model_validate
       (validation error → exit 1, JSON error to stdout)
       
3.   manager = TunnelManager(schema)
4.   results = manager.start_all()        # ThreadPoolExecutor, up to 10 workers
       for each node:
         SSHTunnelForwarder(...)
         tunnel.start()
         verify socket.connect_ex(local_port) succeeds
         record ConnectionEntry list
       on per-node failure: StartResult.error
       
5.   required_failed = [n for n in results if n in required and failed]
       if required_failed:
         manager.stop_all()
         output {error, message, details}
         exit 2
       
6.   build OutputSchema:
       connections = {n: r.connections for n in results if success}
       warnings = [(n, err) for n in results if optional+failed]
       pid = os.getpid()
       started_at = utcnow().isoformat() + "Z"
       
7.   print(output.model_dump_json())
     sys.stdout.flush()
     
8.   daemon.daemonize(log_file)             ─┐
     ├── first fork: parent exits 0          │ caller's `garuda-tunnel start`
     ├── setsid                              │ subprocess returns here
     ├── second fork: session leader exits   │ with exit code 0
     ├── redirect stdin/stdout/stderr        │
     └── continue as detached daemon       ──┘
                                              ↓
9.                                          install SIGTERM/SIGINT handlers
                                            signal.pause()                    ←── waits forever
                                            
caller resumes:                              
   read stdout JSON                          
   pid = json["pid"]                         
   ... do work using tunnels ...             
   
N. garuda-tunnel stop --pid PID    ─────→   SIGTERM received
                                            handler:
                                              manager.stop_all()   ─────→ close all SSHTunnelForwarder
                                              sys.exit(0)
                                            daemon exits
   stop subprocess returns 0
   {stopped: true}
```

**Critical**: the parent process writes the JSON to stdout and flushes
**before** the first fork. By the time the daemon detaches, the caller has
already received the output. The daemon then runs silently with stdout
redirected to the log file (or `/dev/null`).

### Concurrency model

**Within a single `start` invocation:**

- Tunnel startup is concurrent: `ThreadPoolExecutor(max_workers=min(N, 10))`.
- Each worker creates one `SSHTunnelForwarder` and calls `.start()`.
  `SSHTunnelForwarder` itself spawns paramiko transport threads.
- Aggregate wall-clock startup: ~max(per-tunnel-startup), not sum.
- Verification (socket connect) also in parallel after start.

**Daemon runtime:**

- Single Python process, blocked on `signal.pause()` in the main thread.
- Each `SSHTunnelForwarder` runs its own paramiko transport + socketserver
  threads. The daemon process is essentially a thread supervisor.

**Across `start` invocations:**

- No coordination. Each call is an independent process group.
- Two concurrent `start` calls with overlapping node sets → two independent
  daemons, two SSH sessions, OS allocates distinct local ports.

### Error model and exit codes

| Exit | Meaning |
|---|---|
| `0` | Success: `start` daemonized; `stop` completed; `status` reported. |
| `1` | Schema validation failure (input JSON malformed or violates constraints). |
| `2` | Required tunnel(s) failed to start (cleanup performed before exit). |
| `3` | I/O error (stdin read, stdout write, log file open). |
| `4` | Daemon setup error (fork, signal install, FD redirect). |
| `64` | Click usage error (invalid CLI flags). |

All errors also write structured JSON to stdout:

```json
{
  "error": "<ExceptionClassName>",
  "message": "<human readable>",
  "details": { ... per-exception fields ... }
}
```

### Daemonization (POSIX double-fork)

```python
def daemonize(log_file: Path | None) -> None:
    if os.fork() > 0:
        os._exit(0)              # first parent
    os.setsid()
    os.umask(0)
    if os.fork() > 0:
        os._exit(0)              # session leader

    sys.stdout.flush()
    sys.stderr.flush()

    target = open(log_file, "ab", buffering=0) if log_file else open(os.devnull, "ab")
    os.dup2(open(os.devnull, "rb").fileno(), sys.stdin.fileno())
    os.dup2(target.fileno(), sys.stdout.fileno())
    os.dup2(target.fileno(), sys.stderr.fileno())
```

---

## CLI / API contract

### Commands

| Command | Reads stdin | Writes stdout | Daemonizes | Purpose |
|---|---|---|---|---|
| `start` | Yes (JSON) | Yes (JSON) | Yes | Open tunnels, return mapping, detach. |
| `stop`  | No          | Yes (JSON) | No  | SIGTERM by PID, escalate to SIGKILL after grace. |
| `status`| No          | Yes (JSON) | No  | Check if PID is alive. |

### `start` — input schema

```python
class NodeInput(BaseModel):
    host: str                                   # IP or hostname
    port: int = 22                              # SSH port
    user: str
    ssh_pkey: str | None = None                 # PEM content (one of pkey/password required)
    ssh_password: str | None = None
    ssh_pkey_passphrase: str | None = None      # if pkey is encrypted
    remote_ports: list[int]                     # ports on remote 127.0.0.1 to forward
    local_ports: list[int] | None = None        # explicit local ports; None = auto-allocate
    ssh_options: SSHOptions = SSHOptions()

class SSHOptions(BaseModel):
    compression: bool = False
    host_key_policy: Literal["auto", "reject", "warning"] = "auto"
    known_hosts_path: str | None = None
    connect_timeout: int = 60
    threaded: bool = True

class DaemonOptions(BaseModel):
    log_file: str | None = None                 # None → /dev/null
    shutdown_grace_seconds: int = 10

class InputSchema(BaseModel):
    nodes: dict[str, NodeInput]
    require: Literal["*"] | list[str] = "*"
    daemon: DaemonOptions = DaemonOptions()

    @field_validator("require")
    def _validate_require(cls, v, info):
        if v == "*":
            return v
        nodes = info.data.get("nodes", {})
        unknown = set(v) - set(nodes.keys())
        if unknown:
            raise ValueError(f"require references unknown nodes: {sorted(unknown)}")
        return v

    @field_validator("nodes")
    def _validate_auth(cls, v):
        for name, node in v.items():
            if not node.ssh_pkey and not node.ssh_password:
                raise ValueError(f"node {name!r}: must provide ssh_pkey or ssh_password")
        return v
```

### `start` — output schema (success)

```python
class ConnectionEntry(BaseModel):
    remote_host: str
    remote_port: int
    local_host: str
    local_port: int

class TunnelWarning(BaseModel):
    node: str
    error: str
    skipped: bool = True

class OutputSchema(BaseModel):
    connections: dict[str, list[ConnectionEntry]]
    pid: int
    started_at: str                             # ISO 8601 UTC
    warnings: list[TunnelWarning] = []
```

### `start` — output schema (error)

```python
class ErrorOutput(BaseModel):
    error: str                                  # exception class name
    message: str
    details: dict
```

### `stop`

```
$ garuda-tunnel stop --pid <PID> [--grace-seconds N]
```

Output (always exit 0, idempotent):

```json
{ "stopped": true }
{ "stopped": true, "forced": true }
{ "stopped": false, "reason": "not found" }
```

Behavior:

1. `os.kill(pid, 0)` → if ProcessLookupError: exit 0 `{stopped: false}`.
2. `os.kill(pid, SIGTERM)`.
3. Poll `os.kill(pid, 0)` every 0.5s up to `grace_seconds` (default 10).
4. If still alive: `os.kill(pid, SIGKILL)`, output `{stopped: true, forced: true}`.
5. Exit 0.

### `status`

```
$ garuda-tunnel status --pid <PID>
```

Output:

```json
{ "alive": true }
{ "alive": false }
```

Behavior: `os.kill(pid, 0)`. No introspection of tunnel internals (no IPC
channel). Exit 0.

### Required vs optional semantics

```
required_nodes := {all node names} if require == "*" else set(require)

after manager.start_all():
  failed_required = {n: result.error for n in results
                     if not result.success and n in required_nodes}
  failed_optional = {n: result.error for n in results
                     if not result.success and n not in required_nodes}

  if failed_required:
    manager.stop_all()                        # cleanup all started tunnels
    output ErrorOutput(error="RequiredTunnelFailure", ...)
    exit 2

  output OutputSchema(
    connections = {n: r.connections for n in results if r.success},
    warnings    = [TunnelWarning(n, err) for n, err in failed_optional],
  )
  daemonize and wait
```

---

## Testing

Two-layer strategy. Real behavior over mocks.

### Layer 1: Unit (runs on every push)

Path: `tests/unit/`.

- `test_schemas.py` — pure pydantic validation:
  - Valid inputs parse correctly.
  - Missing required fields → `ValidationError`.
  - `require` references unknown node → `ValidationError`.
  - Both `ssh_pkey` and `ssh_password` absent → `ValidationError`.
  - `require: "*"` and `require: list` both parse.

- `test_cli_parsing.py` — Click `CliRunner`, **no subprocess, no real I/O**:
  - `--help` returns 0.
  - `--version` returns 0 with version string.
  - Unknown subcommand → exit 64.
  - `stop --pid <bad-int>` → exit 64.

No mocked manager tests. No mocked daemon tests. Mocks of the
`sshtunnel`/`paramiko` library produce false confidence. Real behavior is
covered in the integration layer below.

Wall clock: <3 seconds. Runs on Python 3.10, 3.11, 3.12, 3.13 ×
Linux+macOS.

### Layer 2: Integration (runs on every push, Linux only)

Path: `tests/integration/`.

Fixture (`conftest.py`):

```python
@pytest.fixture(scope="session")
def ssh_test_cluster(tmp_path_factory):
    """Spin up 2-3 sshd containers with iperf3 on 127.0.0.1:6443 inside each.
    Yield dict: {"nodes": {name: NodeInput-compatible dict}}.
    Tears down on session end.
    """
```

Compose stack (`docker-compose.yml`): N `linuxserver/openssh-server`
containers, each running an `iperf3 -s -B 127.0.0.1 -p 6443` listener as a
trivial TCP target. SSH host port auto-allocated by Docker. Test pubkey
generated per session and injected via `PUBLIC_KEY` env var.

Tests (`test_*.py`) invoke `garuda-tunnel` as a **subprocess** (`subprocess.run`
or `subprocess.Popen`). They verify real behavior:

- `test_start.py`:
  - `require: "*"` all reachable → exit 0, all in `connections`, daemon
    alive at returned PID, `socket.create_connection(127.0.0.1, port)`
    succeeds.
  - `require: ["a", "b"]` with `b` having bad SSH key → exit 2, structured
    error, no orphan daemon (`pgrep garuda-tunnel` empty).
  - `require: ["a"]` with `b` (optional) failing → exit 0, `warnings`
    contains `b`, `connections` has `a` only.
  - Schema validation failure (missing `host`) → exit 1, ErrorOutput.

- `test_stop.py`:
  - Stop alive daemon → `{stopped: true}`, PID gone, tunnel port refuses
    connection.
  - Stop already-dead PID → `{stopped: false, reason: "not found"}`.
  - Stop daemon that ignores SIGTERM (simulated via separate test fixture)
    → `{stopped: true, forced: true}` after grace.

- `test_status.py`:
  - Alive daemon → `{alive: true}`.
  - After stop → `{alive: false}`.

- `test_multiport.py`:
  - One node with `remote_ports: [6443, 6443]` (two forwards to same
    backend) → output has two `ConnectionEntry`s with distinct local ports.

Autouse cleanup fixture in `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def kill_orphan_garuda_tunnel():
    yield
    subprocess.run(["pkill", "-f", "garuda-tunnel"], capture_output=True)
```

Wall clock: ~30 seconds total (5s docker spin-up amortized over session).

### CI matrix

```yaml
# .github/workflows/test.yml (sketch)
jobs:
  unit:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.10", "3.11", "3.12", "3.13"]
    steps:
      - checkout
      - setup-python
      - pip install -e ".[dev]"
      - pytest -m "not integration"
      - mypy --strict garuda_tunnel
      - ruff check
      - ruff format --check

  integration:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.12"]
    steps:
      - checkout
      - setup-python
      - pip install -e ".[dev]"
      - pytest -m integration
```

### Coverage target

`pytest-cov` reports, soft target ≥75% lines. The integration layer
exercises most code paths; mock-free unit tests cover schemas and CLI
parsing.

---

## Distribution and versioning

### Version scheme

`v{YYYY}.1{MM}{DD}.1{HH}{MM}` (UTC), where:

- `1{MM}{DD}` is the literal digit `1`, then zero-padded month (2 digits),
  then zero-padded day (2 digits). Total: 5 digits, always starting with
  `1`.
- `1{HH}{MM}` is the literal digit `1`, then zero-padded hour (2 digits),
  then zero-padded minute (2 digits). Total: 5 digits, always starting
  with `1`.

Examples:
- `v2026.10516.11430` = released 2026-05-16 14:30 UTC.
- `v2026.11225.10001` = released 2026-12-25 00:01 UTC.

Properties:
- Parseable by SemVer-compliant tools as `MAJOR.MINOR.PATCH`.
- Lexicographic sort = chronological sort.
- Unique per minute (sufficient for our release rate).
- Leading `1` prevents leading-zero loss during integer parsing.

### Distribution

Primary: `pipx run --spec git+https://github.com/AlexMKX/garuda-tunnel.git@<TAG> garuda-tunnel ...`.

Secondary: `pipx install` for developer convenience.

Not publishing to PyPI initially. Add later if external adoption justifies
the maintenance overhead.

### Release process

1. Develop on `main`.
2. When releasing:
   ```
   git tag "v$(date -u +'%Y.1%m%d.1%H%M')"
   git push --tags
   ```
3. GitHub Action `release.yml` (on tag push):
   - Run unit + integration tests.
   - Build sdist + wheel via `python -m build`.
   - Create GitHub Release with auto-generated notes.
   - Attach sdist + wheel as release assets.
   - Do not publish to PyPI.

### Backwards compatibility

Pre-1.0 (this period): no SemVer guarantees. Breaking changes documented in
release notes. Production callers pin to a specific tag.

At v1.0+ (if ever): strict SemVer.

### `pyproject.toml`

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
    "paramiko>=4.0,<5",                 # paramiko 5 removed DSSKey; sshtunnel #302 not yet released
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

Note: `pytest-docker` is **not** required as a Python dependency. Tests
invoke `docker compose` directly via `subprocess.run`, sidestepping the
plugin's quirks (port binding races, fixture scoping).

---

## Decisions and rationales

| Decision | Rationale |
|---|---|
| Separate repo, not vendored into garuda-repo | Independent versioning, public OSS reusability, decoupled CI, smaller PRs per change. |
| `pipx run` (ephemeral), not `pipx install` | DE-friendly: no persistent install footprint, version pin per invocation. |
| `pahaz/sshtunnel` library | 1.3k stars, 10+ years prod use, MIT, exact use case fit; better than reinventing subprocess-based ssh -L management. |
| `paramiko<5` pin | Upstream sshtunnel 0.4.0 hits `paramiko.DSSKey` which was removed in paramiko 5.0 (sshtunnel #302). Until upstream fix released, pin to paramiko 4.x. |
| Pydantic for schemas | Type-safe I/O, generated JSON schema for docs, validation errors with structured details. |
| Click for CLI | Idiomatic Python CLI, testable via `CliRunner`, mature. |
| POSIX double-fork daemonization | Standard pattern. systemd-notify and similar are overkill for our DE use case. |
| Required-vs-optional via `require: "*" \| list` | Lets caller distinguish critical tunnels (must succeed or abort) from best-effort ones (failure → warning). Default `"*"` is the conservative behavior. |
| Cleanup all-or-nothing on required failure | Avoids leaking orphan tunnels when the operation as a whole failed. Caller can retry cleanly. |
| Real Docker-based integration tests over mocks | Mocks of `sshtunnel`/`paramiko` give false confidence. Containerized `openssh-server + iperf3` exercises the real code path. |
| Date-based versioning (`vYYYY.1MMDD.1HHMM`) | Lexicographic sort = chronological. SemVer-parseable shape. Unambiguous mapping to release time. |
| MIT license | Most permissive, matches upstream `sshtunnel`. |
| No PyPI publish initially | Reduces maintenance overhead during pilot. `pipx run --spec git+` works fine for our consumer. |
| stdin/stdout JSON, no temp files for secrets | No persistent on-disk artifacts containing SSH keys; daemon process holds them in memory only. |
| Status command minimal (PID liveness only) | Tunnel-internal health introspection from outside the daemon needs IPC. YAGNI for pilot; logs cover diagnostics. |
