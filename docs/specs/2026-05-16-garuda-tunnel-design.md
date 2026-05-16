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
   metadata immediately; `stop --pid --token` cleanly tears down;
   `status --pid [--token]` reports liveness. Suitable for Terragrunt
   `before_hook`/`after_hook` wrapping or any external supervisor with
   PID-based control.

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
- `stop --pid N --token T` kills daemon, all tunnels cleaned, exit 0.
- `status --pid N --token T` returns `{alive: true/false}`.
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
| `cli.py` | Click commands; read stdin / write stdout JSON; remap Click usage errors to exit 64; map domain exceptions to exit codes. |
| `schemas.py` | Pydantic models: `NodeInput`, `SSHOptions`, `DaemonOptions`, `InputSchema`, `ConnectionEntry`, `TunnelWarning`, `OutputSchema`, `ErrorOutput`. |
| `manager.py` | `TunnelManager`: holds list of `sshtunnel.SSHTunnelForwarder` instances; `start_all()` opens concurrently via `ThreadPoolExecutor`; aggregates per-node `StartResult`; `stop_all()` for cleanup; parses inline PEM keys in memory. |
| `daemon.py` | `spawn_daemon(schema)`: POSIX double-fork with an IPC pipe; after the second fork the pre-daemon `execve`s itself into `python -m garuda_tunnel._worker --ipc-fd <N>` with `GARUDA_TUNNEL_TOKEN=<token>` in the new envp so the kernel snapshots the token into `/proc/<pid>/environ`; the worker then starts tunnels, sends startup result and final PID to parent, redirects stdin/stdout/stderr, installs SIGTERM/SIGINT handlers, then blocks on `threading.Event().wait()`. |
| `_worker.py` | Internal entry point invoked via `python -m garuda_tunnel._worker`. Reads `InputSchema` JSON from stdin, runs `TunnelManager.start_all_and_build_output`, writes the IPC message to the file descriptor passed via `--ipc-fd`, and blocks on signals. Not part of the public CLI. |
| `exceptions.py` | Hierarchy: `GarudaTunnelError` → `SchemaValidationError`, `TunnelStartupError`, `RequiredTunnelFailure`, `DaemonError`. Each maps to a specific exit code. |
| `__main__.py` | `python -m garuda_tunnel` → `cli.main()`. |
| `__init__.py` | Exports `__version__` from `importlib.metadata`. No public Python API. |

### Data flow: `start`

```
caller                                      final daemon process
------                                      --------------------
1. echo $INPUT | garuda-tunnel start
2.   cli.start():
       parse stdin → dict
       schemas.InputSchema.model_validate
       (validation error → exit 1, JSON error to stdout)

3.   create IPC pipe (ipc_read_fd, ipc_write_fd)
     create schema pipe (schema_read_fd, schema_write_fd)
     generate runtime_token = secrets.token_urlsafe(32)
4.   first fork:
       parent keeps ipc_read_fd, closes schema_read_fd,
         writes InputSchema JSON to schema_write_fd, closes it,
         waits for one IPC JSON message
       child continues, closes ipc_read_fd and schema_write_fd
5.   child: setsid(); second fork
       session leader exits 0
       pre-daemon continues holding ipc_write_fd and schema_read_fd
6.                                           pre-daemon execve's itself:
                                               os.execve(
                                                 sys.executable,
                                                 [sys.executable, "-m",
                                                  "garuda_tunnel._worker",
                                                  "--ipc-fd", str(ipc_write_fd),
                                                  "--schema-fd", str(schema_read_fd)],
                                                 env={..., "GARUDA_TUNNEL_TOKEN": runtime_token},
                                               )
                                             (the new envp contains the token,
                                              so /proc/<pid>/environ will show it)
                                              ↓
7.                                           worker process starts:
                                               parse argv (--ipc-fd, --schema-fd)
                                               redirect stdin/stdout/stderr to /dev/null or log file
                                               read InputSchema JSON from schema_read_fd, close it
                                               manager = TunnelManager(schema)
8.                                           results = manager.start_all()
                                               # ThreadPoolExecutor, up to 10 workers
                                               for each node:
                                                 parse inline PEM in memory if provided
                                                 SSHTunnelForwarder(...)
                                                 tunnel.start()
                                                 verify socket.connect_ex(local_port)
                                                 record ConnectionEntry list
                                               on per-node failure: StartResult.error

9.                                           required_failed = [n for n in results
                                               if n in required and failed]
                                             if required_failed:
                                               manager.stop_all()
                                               write ErrorOutput to ipc_write_fd
                                               exit 2

10.                                          build OutputSchema:
                                               connections = successful results
                                               warnings = optional failures
                                               pid = os.getpid()  # final daemon PID
                                               token = runtime_token (from environ)
                                               started_at = utcnow().isoformat() + "Z"
                                             write OutputSchema to ipc_write_fd
                                             close ipc_write_fd
11.                                          install SIGTERM/SIGINT handlers
                                             threading.Event().wait()          ←── waits forever

12.  parent reads IPC JSON:
       if success:
         print OutputSchema to stdout
         exit 0
       if daemon reported required failure:
         print ErrorOutput to stdout
         exit 2
       if daemon failed before reporting:
         print DaemonError to stdout
         exit 4

caller resumes:
   read stdout JSON
   pid = json["pid"]
   token = json["token"]
   ... do work using tunnels ...

N. garuda-tunnel stop --pid PID --token TOKEN ─────→ SIGTERM received
                                                   handler:
                                                     manager.stop_all() ─────→ close all SSHTunnelForwarder
                                                     sys.exit(0)
                                                   daemon exits
   stop subprocess returns 0
   {stopped: true}
```

**Critical**: the daemon forks AND re-execs before any `SSHTunnelForwarder.start()`
call. `SSHTunnelForwarder` starts Paramiko and socketserver threads; forking
after those threads exist is unsafe because only the calling thread survives
in the child. The final worker process must create and own all tunnel threads.

**Critical**: the parent process writes stdout only after the worker has
reported its final PID and startup result through the IPC pipe. The PID in
`OutputSchema` is therefore the PID that `stop` and `status` should target.

**Critical**: the runtime token is placed in the worker's environment via
`os.execve(..., env={..., GARUDA_TUNNEL_TOKEN: token})`, not via
`os.environ[...]=token` after fork. On Linux the kernel snapshots `envp` at
`execve(2)` and exposes it via `/proc/<pid>/environ`; variables added via
`setenv(3)` (which Python's `os.environ` uses) after exec live in libc's heap
and are not visible to `/proc/<pid>/environ`. The execve step is what makes
the identity check usable.

### Concurrency model

**Within a single `start` invocation:**

- Parent process performs only JSON parsing/validation, daemon forking, IPC
  wait, and stdout output. It never starts SSH tunnels.
- Tunnel startup is concurrent: `ThreadPoolExecutor(max_workers=min(N, 10))`.
- Each worker creates one `SSHTunnelForwarder` and calls `.start()`.
  `SSHTunnelForwarder` itself spawns paramiko transport threads.
- Aggregate wall-clock startup: ~max(per-tunnel-startup), not sum.
- Verification (socket connect) also in parallel after start.

**Daemon runtime:**

- Single Python worker process, blocked on `threading.Event().wait()` in the main
  thread.
- Each `SSHTunnelForwarder` runs its own paramiko transport + socketserver
  threads. The worker process is essentially a thread supervisor.

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

### Daemonization (POSIX double-fork plus execve)

```python
def spawn_daemon(schema: InputSchema) -> dict:
    ipc_read_fd, ipc_write_fd = os.pipe()
    schema_read_fd, schema_write_fd = os.pipe()
    runtime_token = secrets.token_urlsafe(32)

    first_pid = os.fork()
    if first_pid > 0:
        # Parent: send schema down to the worker via the schema pipe, then
        # wait for the worker to report startup result via the ipc pipe.
        os.close(ipc_write_fd)
        os.close(schema_read_fd)
        os.write(schema_write_fd, schema.model_dump_json().encode("utf-8"))
        os.close(schema_write_fd)
        return read_one_json_message(ipc_read_fd)  # worker PID + startup result

    # First child.
    os.close(ipc_read_fd)
    os.close(schema_write_fd)
    os.setsid()
    os.umask(0)
    if os.fork() > 0:
        os._exit(0)              # session leader

    # Pre-daemon: replace process image with the worker, putting the token in
    # the new envp so /proc/<pid>/environ can see it after exec.
    env = {**os.environ, GARUDA_TUNNEL_TOKEN_ENV: runtime_token}
    os.execve(
        sys.executable,
        [
            sys.executable, "-m", "garuda_tunnel._worker",
            "--ipc-fd", str(ipc_write_fd),
            "--schema-fd", str(schema_read_fd),
        ],
        env,
    )
```

```python
# garuda_tunnel/_worker.py
def main() -> None:
    args = parse_args()                                 # --ipc-fd, --schema-fd
    token = os.environ[GARUDA_TUNNEL_TOKEN_ENV]         # set by parent's execve
    schema_json = os.read(args.schema_fd, MAX_SCHEMA_BYTES).decode("utf-8")
    os.close(args.schema_fd)
    schema = InputSchema.model_validate_json(schema_json)

    redirect_standard_fds_to_log_or_devnull(schema.daemon.log_file)

    manager = None
    try:
        manager = TunnelManager(schema)
        startup_result = manager.start_all_and_build_output(
            pid=os.getpid(),
            token=token,
        )
        write_json_message(args.ipc_fd, startup_result)
        if startup_result.get("error"):                  # ErrorOutput payload
            if manager is not None:
                manager.stop_all()
            os._exit(2)
    finally:
        os.close(args.ipc_fd)

    install_signal_handlers(manager)
    wait_for_signal()
```

The pre-daemon never returns from `os.execve`; the kernel replaces the process
image with the freshly-loaded Python interpreter, which runs `_worker.main()`
in the same process ID as the second-fork child. `/proc/<pid>/environ` now
contains the snapshot of `env` passed to `execve`, including
`GARUDA_TUNNEL_TOKEN=<token>`.

---

## CLI / API contract

### Commands

| Command | Reads stdin | Writes stdout | Daemonizes | Purpose |
|---|---|---|---|---|
| `start` | Yes (JSON) | Yes (JSON) | Yes | Open tunnels, return mapping, detach. |
| `stop`  | No          | Yes (JSON) | No  | Verify PID+token identity, SIGTERM, escalate to SIGKILL after grace. |
| `status`| No          | Yes (JSON) | No  | Check if PID is alive, optionally scoped by token identity. |

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
    token: str                                  # runtime identity token for safe stop/status
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
$ garuda-tunnel stop --pid <PID> --token <TOKEN> [--grace-seconds N]
```

Output (always exit 0, idempotent):

```json
{ "stopped": true }
{ "stopped": true, "forced": true }
{ "stopped": false, "reason": "not found" }
```

Behavior:

1. `os.kill(pid, 0)` → if ProcessLookupError: exit 0 `{stopped: false}`.
2. Verify that the process is a `garuda-tunnel` daemon for the provided token.
   - Linux: read `/proc/<pid>/environ` and require
     `GARUDA_TUNNEL_TOKEN=<TOKEN>`.
   - macOS: use `ps -wwE -p <pid>` and require the same environment marker
     when available. If the platform cannot verify the token, return
     `{stopped: false, reason: "identity check unavailable"}` and exit 0;
     do not kill.
3. `os.kill(pid, SIGTERM)`.
4. Poll `os.kill(pid, 0)` every 0.5s up to `grace_seconds` (default 10).
5. If still alive: re-check identity, then `os.kill(pid, SIGKILL)`, output
   `{stopped: true, forced: true}`.
6. Exit 0.

The token prevents killing unrelated processes when a PID is stale, reused, or
typed incorrectly. The daemon sets `GARUDA_TUNNEL_TOKEN` in its own process
environment before reporting success through IPC.

### `status`

```
$ garuda-tunnel status --pid <PID> [--token <TOKEN>]
```

Output:

```json
{ "alive": true }
{ "alive": false }
```

Behavior: `os.kill(pid, 0)`. If `--token` is provided, also perform the same
identity check used by `stop`; token mismatch returns `{alive: false}`. No
introspection of tunnel internals (no IPC channel). Exit 0.

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
    pid         = os.getpid(),
    token       = runtime_token,
  )
  write output to parent over IPC, then wait for signals
```

### Inline PEM handling

`ssh_pkey` is PEM content, not a file path. The implementation must parse it in
memory and must not write the private key to a temporary file.

Implementation approach:

1. Wrap the PEM string in `io.StringIO`.
2. Try Paramiko key classes in a deterministic order, for example
   `Ed25519Key`, `ECDSAKey`, `RSAKey`, `DSSKey` while Paramiko 4 still exposes
   it.
3. Pass the parsed key object to `SSHTunnelForwarder` through the appropriate
   Paramiko/sshtunnel argument rather than passing a path.
4. If `ssh_pkey` and `ssh_password` are both provided, prefer key auth and keep
   password available only if `sshtunnel` supports fallback without writing
   secrets.
5. Error output must not include the PEM content or password.

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

Click defaults usage errors to exit code 2. The CLI must explicitly remap
`click.UsageError` / bad parameter errors to exit 64 so the behavior is stable
and testable.

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
    alive at returned PID, `token` present, `socket.create_connection(127.0.0.1,
    port)` succeeds.
  - `require: ["a", "b"]` with `b` having bad SSH key → exit 2, structured
    error, no orphan daemon among processes started by this pytest run.
  - `require: ["a"]` with `b` (optional) failing → exit 0, `warnings`
    contains `b`, `connections` has `a` only.
  - Schema validation failure (missing `host`) → exit 1, ErrorOutput.

- `test_stop.py`:
  - Stop alive daemon → `{stopped: true}`, PID gone, tunnel port refuses
    connection.
  - Stop alive daemon with wrong token → `{stopped: false, reason: ...}`, PID
    still alive.
  - Stop already-dead PID → `{stopped: false, reason: "not found"}`.
  - Stop daemon that ignores SIGTERM (simulated via separate test fixture)
    → `{stopped: true, forced: true}` after grace.

- `test_status.py`:
  - Alive daemon → `{alive: true}`.
  - Alive daemon with wrong token → `{alive: false}`.
  - After stop → `{alive: false}`.

- `test_multiport.py`:
  - One node with `remote_ports: [6443, 6443]` (two forwards to same
    backend) → output has two `ConnectionEntry`s with distinct local ports.

Autouse cleanup fixture in `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def kill_orphan_test_daemons(started_daemons):
    yield
    # Kill only daemons started by this pytest process. The fixture records
    # (pid, token) pairs returned by successful `start` calls.
    # Never use pkill -f "garuda-tunnel" because that can kill unrelated local
    # operator processes on a developer machine.
    for pid, token in started_daemons:
        subprocess.run(["garuda-tunnel", "stop", "--pid", str(pid), "--token", token])
```

Integration tests keep the `pid` and `token` returned by each production
`start` call and use those values for targeted cleanup. There is no test-only
input schema branch for overriding tokens.

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
| Fork before starting tunnels | `SSHTunnelForwarder.start()` creates Paramiko/socketserver threads. Forking after thread creation is unsafe, so the final worker must own startup. |
| `execve` after double-fork instead of `os.environ[...] = token` | Linux `/proc/<pid>/environ` is the kernel's snapshot of `envp` taken at `execve(2)`; `setenv(3)` (and Python's `os.environ`) updates only libc's copy after that snapshot is taken. To make the runtime token visible to `stop --pid --token` via `/proc/<pid>/environ`, the daemon re-execs itself with the token in the fresh `envp`. |
| Parent/worker IPC pipe | Lets `start` return the final worker PID and startup result while keeping stdout clean and machine-readable. |
| Separate schema pipe parent → worker | The worker is a freshly execve'd process and cannot inherit the in-memory `InputSchema` from the parent. The parent serializes the schema to JSON on a second pipe; the worker reads it on stdin-side, validates, and runs. |
| PID plus token for stop/status | PID alone can target an unrelated reused process. A runtime token in the daemon environment gives callers a stateless identity check. |
| Real Docker-based integration tests over mocks | Mocks of `sshtunnel`/`paramiko` give false confidence. Containerized `openssh-server + iperf3` exercises the real code path. |
| Date-based versioning (`vYYYY.1MMDD.1HHMM`) | Lexicographic sort = chronological. SemVer-parseable shape. Unambiguous mapping to release time. |
| MIT license | Most permissive, matches upstream `sshtunnel`. |
| No PyPI publish initially | Reduces maintenance overhead during pilot. `pipx run --spec git+` works fine for our consumer. |
| stdin/stdout JSON, no temp files for secrets | No persistent on-disk artifacts containing SSH keys; daemon process parses inline PEM in memory and holds key objects only in memory. |
| Status command minimal (PID liveness only) | Tunnel-internal health introspection from outside the daemon needs IPC. YAGNI for pilot; logs cover diagnostics. |
