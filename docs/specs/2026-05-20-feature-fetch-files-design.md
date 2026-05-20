# garuda-tunnel: asyncssh + fetch_files + remote_targets + identity lockfile + subprocess IPC

**Status:** final (state as merged in this PR)
**Repository:** https://github.com/AlexMKX/garuda-tunnel
**Branch (this PR):** `feature/fetch-files`
**Supersedes:** the `sshtunnel`/`paramiko` transport, the `connections[node]: list[ConnectionEntry]` output, the `InputSchema.require` global selector, the `remote_ports`/`local_ports` input fields, the `/proc/<pid>/environ`-based identity check, and the hand-rolled `fork+setsid+execve` daemonization that preceded this work in `2026-05-16-garuda-tunnel-design.md`.

## 1. Goal

A single coordinated release that ships:

1. `asyncssh` transport replacing `sshtunnel`/`paramiko`.
2. `fetch_files` feature — read small remote files over the same SSH session as the tunnel.
3. `remote_targets` schema — handle-keyed `dict[str, "host:port"]` replacing positional `remote_ports`/`local_ports`. Enables bastion-style cross-host forwards.
4. Per-node `required: bool` replacing the global `InputSchema.require` selector.
5. `fcntl.flock`-based identity check on `~/.local/state/garuda-tunnel/<token>.lock` replacing `/proc/<pid>/environ` parsing.
6. `subprocess.Popen(start_new_session=True, pass_fds=[...])` IPC replacing hand-rolled `os.fork()` + `os.setsid()` + second `os.fork()` + `os.execve()`.
7. User-facing README rewrite.
8. Toolchain: `black`, `pylint`, `vulture`, `mypy --strict`, combined unit + integration coverage gate at 80%.

These eight items ship together because they all touch the public input/output contract or the daemon's process model, and the migration from `sshtunnel` to `asyncssh` enabled the rest.

## 2. Why

- `sshtunnel==0.4.0` is unmaintained (no PyPI release since 2021). Access to `paramiko.Transport` requires reaching into a private `_transport` attribute. Multiplexing SFTP and forwards over one session was impossible without leaning on internals.
- The motivating use case is k3s-style edge bootstrap: a CI / Terragrunt run needs both a port forward to `127.0.0.1:6443` *and* the kubeconfig from `/etc/rancher/k3s/k3s.yaml`. Today this requires a second SSH session and a second authentication; the daemon already holds an open session that can multiplex SFTP at zero extra cost.
- Positional `remote_ports: list[int]` is brittle for automation (downstream `jq` must count list positions to find a forward) and hard-codes `127.0.0.1` as the remote bind host, precluding bastion-style forwards into an internal network.
- The `/proc/<pid>/environ` identity check races on CI runners (the kernel populates `environ` asynchronously) and leaks the token to any user who can run `ps eww <pid>`.
- The hand-rolled `os.fork() + os.setsid() + os.fork() + os.execve()` pattern in `daemon.py` had a flaky IPC handshake on CI that manifested as "worker IPC pipe closed without a message" intermittent failures.

## 3. Non-goals

- Async public CLI: `garuda-tunnel start | stop | status` stays synchronous from the caller's perspective.
- Control-plane Unix socket. `stop` and `status` keep the existing pid + token over signal model.
- Host-key pinning. `known_hosts=None` matches the prior `sshtunnel` behavior; pinning is a separate spec.
- Per-target `required` flag. Node-level `required` still gates the whole node.
- IPv6 in the local bind host (always `127.0.0.1`).
- Binding local listeners on non-loopback (e.g. `0.0.0.0`).
- Windows support. `fcntl.flock` and `start_new_session` are POSIX-only. Windows works via WSL only.
- Persistent SSH session pool, multi-process worker, or fan-out to N>1 SSH connections per node.
- Remote command execution. SFTP read-only, no shell.
- Async/streaming for `fetch_files` content (one-shot, fully buffered, ≤1 MiB).

## 4. Public schema

### 4.1 Input

```jsonc
{
  "nodes": {
    "edge1": {
      "host": "198.51.100.10",
      "port": 22,
      "user": "tester",
      "ssh_pkey": "<PEM>",
      "ssh_password": null,
      "ssh_pkey_passphrase": null,
      "ssh_options": {
        "compression": false,
        "connect_timeout": 60
      },
      "remote_targets": {
        "kubeapi":    "10.0.0.1:6443",
        "prometheus": "10.0.0.2:9090"
      },
      "required": true,
      "fetch_files": {
        "kubeconfig": {"path": "/etc/rancher/k3s/k3s.yaml", "required": true}
      }
    }
  },
  "daemon": {
    "log_file": null,
    "shutdown_grace_seconds": 10
  }
}
```

#### NodeInput

| Field | Type | Default | Notes |
|---|---|---|---|
| `host` | `str` | required | SSH server host or IP. |
| `port` | `int` | `22` | SSH server port. |
| `user` | `str` | required | SSH user. |
| `ssh_pkey` | `str \| null` | `null` | PEM-encoded private key (in-memory; never written). |
| `ssh_password` | `str \| null` | `null` | Password fallback. Either `ssh_pkey` or `ssh_password` must be set. |
| `ssh_pkey_passphrase` | `str \| null` | `null` | Optional passphrase for `ssh_pkey`. |
| `ssh_options.compression` | `bool` | `false` | Force zlib SSH compression. |
| `ssh_options.connect_timeout` | `int` | `60` | Seconds. |
| `remote_targets` | `dict[str, str]` | required | 1..16 entries. Each value is `"host:port"`. `host` is resolved on the SSH server, enabling bastion-style cross-host forwards. |
| `required` | `bool` | `true` | If false, this node's startup failure is a `TunnelWarning` instead of aborting `start`. |
| `fetch_files` | `dict[str, FileSpec] \| null` | `null` | 1..16 entries when set. |

`remote_targets` handle (key) regex: `^[a-zA-Z_][a-zA-Z0-9_-]*$`, max 64 chars.

Value `"host:port"` accepts:

| Form | Example | Notes |
|---|---|---|
| IPv4 + port | `"10.0.0.1:6443"` | Most common. |
| DNS name + port | `"node.local:22"` | Resolution happens on the SSH server. |
| IPv6 + port (bracketed) | `"[::1]:6443"` | Brackets are required to disambiguate from the port colon. |
| IPv6 + port (bracketed, full) | `"[2001:db8::1]:443"` | Same. |

Parsing errors are reported as `SchemaValidationError` (exit 1):

| Cause | Message fragment |
|---|---|
| No colon | `missing ':' in target` |
| Empty host | `empty host` |
| Port not in 1..65535 or non-numeric | `port must be 1..65535` |
| IPv6 without brackets | `IPv6 target must use [host]:port form` |

Each error includes the handle and the original value, e.g. `remote_targets["kubeapi"]: "10.0.0.1": missing ':' in target`.

Duplicate `host:port` across handles is allowed — two handles pointing at the same target get two independent local listeners on distinct local ports.

#### FileSpec

| Field | Type | Default | Notes |
|---|---|---|---|
| `path` | `str` | required | Absolute remote path (no `~`, no `$VAR` expansion), 1..4096 chars. |
| `required` | `bool` | `true` | If false, a fetch failure is recorded in `fetch_files[name].error` and does not fail the node. |

`fetch_files` handle (key) regex: `^[a-zA-Z_][a-zA-Z0-9_-]*$`, max 64 chars.

#### DaemonOptions

| Field | Type | Default | Notes |
|---|---|---|---|
| `log_file` | `str \| null` | `null` | Worker stdout/stderr go here (append mode). Never contains fetched content. |
| `shutdown_grace_seconds` | `int` | `10` | SIGTERM grace period before SIGKILL during `stop`. |

### 4.2 Output (success)

```jsonc
{
  "connections": {
    "edge1": {
      "ports": {
        "kubeapi":    54321,
        "prometheus": 54322
      },
      "fetch_files": {
        "kubeconfig": {
          "content_b64": "YXBpVmVyc2lvbjogdjEK...",
          "size": 2918,
          "sha256": "d2a0bf3c..."
        }
      }
    }
  },
  "pid": 12345,
  "token": "<opaque>",
  "started_at": "2026-05-20T10:00:00Z",
  "warnings": []
}
```

- `connections[node].ports` is `dict[str, int]` — handle → local port. Local bind host is always `127.0.0.1`.
- `connections[node].fetch_files[name]` is either `{content_b64, size, sha256}` (success) or `{error}` (failure).
- `warnings` is a list of `{node, error, skipped: true}` entries for optional (`required: false`) nodes that failed to start.

### 4.3 Output (failure)

```jsonc
{
  "error": "RequiredTunnelFailure",
  "message": "required tunnel(s) failed to start",
  "details": {
    "failed": [
      {"node":"edge1","error":"required fetch_files failed: ['kubeconfig']"}
    ]
  }
}
```

Or, on schema validation failure:

```jsonc
{
  "error": "SchemaValidationError",
  "message": "<pydantic error summary>",
  "details": {}
}
```

### 4.4 Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Schema validation failure. |
| 2 | One or more required nodes failed to start. |
| 4 | Daemon-internal error (lock acquisition failed, IPC pipe closed without a message, JSON parse failure, etc). |

### 4.5 `fetch_files` error vocabulary

| Value | Meaning | First remediation |
|---|---|---|
| `SSH_FX_NO_SUCH_FILE` | Path doesn't exist | `ssh user@host ls -la <path>` |
| `SSH_FX_PERMISSION_DENIED` | File ACL blocks the SSH user | Check ownership/mode |
| `SSH_FX_FAILURE` | Generic server-side SFTP failure | Inspect remote sshd logs |
| `SSH_FX_NO_CONNECTION` | SFTP subsystem rejected the channel | Verify `Subsystem sftp` in `sshd_config` |
| `SSH_FX_CONNECTION_LOST` | Channel died mid-read | Network instability; retry |
| `SSH_FX_OP_UNSUPPORTED` | Server doesn't implement the operation | Non-OpenSSH SFTP server; not supported |
| `SSH_FX_EOF` | stat past EOF / similar | Race against truncation; retry |
| `SSH_FX_BAD_MESSAGE` | Protocol-level failure | Likely SSH server bug |
| `SSH_FX_UNKNOWN` | Numeric SFTP code outside 1..8 | Unknown server-side error |
| `EFBIG` | File exceeds the 1 MiB hard cap | Wrong file, or this tool isn't the right transport |
| `ChannelOpenError` / `ConnectionResetError` / `TimeoutError` | Transport-level failure | Network or sshd config issue |
| `RuntimeError` | Internal invariant breach | Bug; check stderr |

## 5. Architecture

### 5.1 Transport: `asyncssh` per node

`garuda_tunnel/ssh.py`:

- `async def open_connection(node: NodeInput) -> asyncssh.SSHClientConnection`
  - `asyncssh.connect(host=node.host, port=node.port, username=node.user, known_hosts=None, client_keys=..., password=..., connect_timeout=node.ssh_options.connect_timeout, keepalive_interval=30)`.
  - Private key loaded in-memory via `asyncssh.import_private_key(node.ssh_pkey, node.ssh_pkey_passphrase)`. Never written to disk.
  - Compression: `compression_algs=("zlib@openssh.com", "zlib")` only if `node.ssh_options.compression=True`; otherwise asyncssh defaults.
- `async def open_local_forwards(conn, node) -> tuple[dict[str, int], list[asyncssh.SSHListener]]`
  - One `conn.forward_local_port("127.0.0.1", 0, target.host, target.port)` call per handle in `node.remote_targets.items()`.
  - Local bind is always `127.0.0.1`, port is OS-assigned (0).
  - TCP probe on the resulting local listener with `node.ssh_options.connect_timeout` to validate the forward accepts connections. Failure → `TunnelStartupError("local forward did not accept connection", {"handle": handle, "target": "<host>:<port>", "local_port": actual_port})`.
  - Returns `(handle → local_port, listeners)`.
  - Cleanup path on any failure: `try: ... except BaseException: await close_transport(None, listeners); raise`. Covers `KeyboardInterrupt` and `CancelledError` to avoid leaking SSH channels.
- `async def close_transport(conn, listeners) -> None`
  - Best-effort teardown: close listeners, then close connection. Never raises. Catches `(asyncssh.Error, OSError)`.

### 5.2 Manager: async lifecycle

`garuda_tunnel/manager.py`:

- `TunnelManager(schema)` exposes:
  - `async def start_all_and_build_output(*, pid, token) -> OutputSchema | ErrorOutput`
  - `async def stop_all() -> None`
- `_NodeRuntime` dataclass:
  - `conn: asyncssh.SSHClientConnection | None`
  - `listeners: list[asyncssh.SSHListener]`
  - `ports: dict[str, int]` — handle → local port (forwarded as `NodeOutput.ports`)
  - `fetched_files: dict[str, FetchedFile]`
- Concurrency: `await asyncio.gather(*[_start_one(n) for n in nodes], return_exceptions=False)`. Per-node exceptions caught inside `_start_one`; `gather` itself never sees an exception.
- Per-node failure semantics:
  - `node.required=True` failure → cancel/cleanup all already-started nodes, return `ErrorOutput(error="RequiredTunnelFailure", details.failed=[...])`. CLI exits 2.
  - `node.required=False` failure → entry omitted from `connections`, `TunnelWarning(node=name, error=str)` appended.
- `_NODE_STARTUP_ERRORS` tuple: `(asyncssh.Error, asyncssh.KeyImportError, OSError, asyncio.TimeoutError, TunnelStartupError)`. Anything else is a programming bug and propagates.

### 5.3 Fetcher: `asyncssh.SFTPClient`

`garuda_tunnel/fetcher.py`:

- `_MAX_FETCH_BYTES: Final[int] = 1 << 20` (1 MiB, private module constant).
- `async def fetch_files(conn, specs) -> tuple[dict[str, FetchedFile], list[str]]`
- Empty `specs` → `({}, [])` without opening SFTP.
- Otherwise opens one SFTP channel multiplexed on the existing `SSHClientConnection`:
  - `stat = await sftp.stat(spec.path)`; if `stat.size > _MAX_FETCH_BYTES` → `FetchedFile(error="EFBIG")`.
  - Otherwise `await fh.read(_MAX_FETCH_BYTES + 1)`; if `len(raw) > _MAX_FETCH_BYTES` → `FetchedFile(error="EFBIG")`.
  - On success: `FetchedFile(content_b64=base64(raw), size=len(raw), sha256=sha256(raw).hexdigest())`.
- Error mapping:
  - `asyncssh.SFTPError.code` 1..8 → canonical `SSH_FX_*` strings.
  - Unknown code → `SSH_FX_UNKNOWN`.
  - Any other expected transport exception (`asyncssh.ChannelOpenError`, `OSError`, `asyncio.TimeoutError`, `ConnectionResetError`) → `FetchedFile(error=type(exc).__name__)`. Narrow tuple `_SFTP_TRANSPORT_ERRORS = (asyncssh.Error, OSError, asyncio.TimeoutError)`.
- If SFTP channel open itself fails → classify once and apply the same error to every entry of `specs`; required entries go to `required_failures`.

### 5.4 Daemon: `subprocess.Popen` + dedicated IPC fd

`garuda_tunnel/daemon.py`:

```python
def spawn_daemon(schema: InputSchema) -> dict[str, Any]:
    _sweep_stale_lockfiles()
    runtime_token = secrets.token_urlsafe(32)
    ipc_read_fd, ipc_write_fd = os.pipe()
    log_target = _open_log_target(schema.daemon.log_file)
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "garuda_tunnel._worker",
                f"--ipc-fd={ipc_write_fd}",
                f"--token={runtime_token}",
            ],
            stdin=subprocess.PIPE,
            stdout=log_target,
            stderr=log_target,
            pass_fds=[ipc_write_fd],
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(ipc_write_fd)
        if not isinstance(log_target, int):
            log_target.close()
    proc.stdin.write(schema.model_dump_json().encode("utf-8"))
    proc.stdin.close()
    return _read_ipc_response(ipc_read_fd)
```

Key properties:

- `start_new_session=True` does the equivalent of `setsid` + reparenting to init in one call. The worker is detached from the parent's controlling terminal.
- `pass_fds=[ipc_write_fd]` is the **only** inherited fd besides 0/1/2. No `--schema-fd` second pipe; schema flows via stdin.
- `stdout=log_target, stderr=log_target` means anything the worker writes to its own stdout/stderr (logger output, `print`, warnings) goes to the log file or `/dev/null`. It does **not** go to the IPC pipe.
- The IPC pipe is a dedicated fd known only to this worker. No third-party library or stray `print` can corrupt the protocol channel.
- Argv uses `f"--key={value}"` form rather than `["--key", value]` so a leading-`-` value (which `secrets.token_urlsafe` produces ~1.7 % of the time) is not interpreted by argparse as a flag.
- Parent never calls `proc.wait()`. The worker is detached, gets reparented to init (PID 1) when the CLI parent exits.

`_open_log_target(path) -> int | IO[bytes]`: returns `subprocess.DEVNULL` (int sentinel) when no log file, or an `open(path, "ab", buffering=0)` handle. Caller is responsible for closing the file in the parent after Popen returns; the worker keeps its own dup'd fd.

`_sweep_stale_lockfiles()`: at the top of `spawn_daemon`, globs `<state_dir>/*.lock`, tries `LOCK_EX | LOCK_NB` on each. Files that acquire are stale (no live daemon holds them) → unlinked. Race-safe against concurrent daemons (a live daemon holds an exclusive flock; sweep's `LOCK_NB` fails for it).

### 5.5 Worker: stdin schema + identity lock

`garuda_tunnel/_worker.py`:

```python
def main(argv=None):
    args = _parse_args(argv)            # --ipc-fd, --token
    try:
        lock_fd = _acquire_identity_lock(args.token)
    except Exception as exc:
        _report_pre_run_failure(args.ipc_fd, exc)
        os._exit(4)
    rc = asyncio.run(_run(args, lock_fd))
    os._exit(rc)


async def _run(args, lock_fd):
    try:
        schema = _read_schema_from_stdin()
    except (DaemonError, ValidationError, ...) as exc:
        _report_pre_run_failure(args.ipc_fd, exc)
        _release_identity_lock(lock_fd, args.token)
        return 4

    manager = TunnelManager(schema)
    try:
        result = await manager.start_all_and_build_output(pid=os.getpid(), token=args.token)
    except Exception as exc:
        await manager.stop_all()
        _report_pre_run_failure(args.ipc_fd, exc)
        _release_identity_lock(lock_fd, args.token)
        return 4

    if isinstance(result, ErrorOutput):
        await manager.stop_all()
        _write_message(args.ipc_fd, {"kind": "required_failure", "payload": ...})
        os.close(args.ipc_fd)
        _release_identity_lock(lock_fd, args.token)
        return 2

    _write_message(args.ipc_fd, {"kind": "success", "payload": ...})
    os.close(args.ipc_fd)

    stop_event = asyncio.Event()
    loop.add_signal_handler(SIGTERM, stop_event.set)
    loop.add_signal_handler(SIGINT, stop_event.set)
    try:
        await stop_event.wait()
    finally:
        await manager.stop_all()
        _release_identity_lock(lock_fd, args.token)
    return 0
```

- Schema is read from stdin (`sys.stdin.buffer.read()`), capped at `_SCHEMA_MAX_BYTES = 8 MiB`.
- Identity lock acquired in `main()` **before** `asyncio.run`. Acquire failure is reported via IPC and exits with code 4. This is critical: if acquire fails after `asyncio.run` started, the worker's top-level guard inside `_run` catches it; if it fails *before* `asyncio.run`, the guard in `main()` writes the IPC frame instead of leaving the parent blocked on an empty pipe.
- No `_redirect_standard_fds` helper. Parent controls stdout/stderr via Popen.

### 5.6 Identity: `fcntl.flock` on `<state_dir>/<token>.lock`

`garuda_tunnel/identity.py`:

```python
def verify_token(pid: int, token: str) -> IdentityCheckResult:
    if not _process_exists(pid):
        return IdentityCheckResult.not_found
    lock_path = _state_dir() / f"{token}.lock"
    if not lock_path.is_file():
        return IdentityCheckResult.not_found
    fd = os.open(lock_path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Lock is held → daemon is alive. Verify recorded PID matches.
            recorded_pid = int(lock_path.read_bytes().strip())
            return (
                IdentityCheckResult.match if recorded_pid == pid
                else IdentityCheckResult.mismatch
            )
        # Got the lock → daemon is dead.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return IdentityCheckResult.not_found
    finally:
        os.close(fd)
```

- State directory: `${XDG_STATE_HOME:-~/.local/state}/garuda-tunnel/`, mode `0o700`.
- Lock file: `<token>.lock`, mode `0o600`, content is daemon's PID as decimal text (diagnostic only; the authority is "holds the flock").
- Daemon (worker) acquires `LOCK_EX | LOCK_NB` at startup and keeps the fd open for its lifetime. The kernel releases the flock automatically on process exit, clean or not.
- On graceful shutdown (`SIGTERM`/`SIGINT` handler in `_run`), the worker also `unlink`s the file and closes the fd.

This replaces the previous environ-based check that parsed `/proc/<pid>/environ` on Linux and `ps -wwE` on macOS. The token is no longer in process environ, so `ps eww` cannot leak it. The mechanism is identical on Linux and macOS (POSIX `flock`).

### 5.7 Schemas

`garuda_tunnel/schemas.py`:

- `RemoteTarget(BaseModel)` with `host: str (1..255)`, `port: int (ge=1, le=65535)`, `extra=forbid`.
- `NodeInput.remote_targets: dict[str, RemoteTarget]` — a `mode="before"` field validator parses each `"host:port"` string via `_parse_host_port(value)` (or accepts an already-parsed `RemoteTarget`, or a dict for `model_dump` round-trip) and replaces the value with a `RemoteTarget`. Enforces 1..16 entries and handle regex.
- `NodeInput.required: bool = True`.
- `NodeInput.fetch_files: dict[str, FileSpec] | None = None`. Validator: 1..16 entries when set; handle regex.
- `FileSpec(BaseModel)` with `path: str (1..4096, absolute, no '~')`, `required: bool = True`, `extra=forbid`.
- `FetchedFile(BaseModel)` with XOR validator: either `(content_b64, size, sha256)` or `(error,)`, never both.
- `NodeOutput(BaseModel)` with `ports: dict[str, int]`, `fetch_files: dict[str, FetchedFile] = {}`.
- `OutputSchema(BaseModel)` with `connections: dict[str, NodeOutput]`, `pid`, `token`, `started_at`, `warnings: list[TunnelWarning]`.
- `ErrorOutput(BaseModel)` with `error`, `message`, `details: dict[str, Any]`.
- `InputSchema` has `extra=forbid` so legacy `require`, `remote_ports`, `local_ports`, removed `SSHOptions` fields (`host_key_policy`, `known_hosts_path`, `threaded`) are rejected at validation time with clear errors.

## 6. Tests

### 6.1 Unit (131 tests)

- `tests/unit/test_schemas*.py` — schema validation, parser edge cases, round-trip via `model_dump`.
- `tests/unit/test_ssh_transport.py` — `open_local_forwards` with mock `asyncssh.SSHClientConnection`; covers handle preservation, cleanup on mid-loop failure, probe failure path.
- `tests/unit/test_fetcher_unit.py` and `test_fetcher_errno_map.py` — fetcher happy paths, ENOENT/EFBIG/permission denied, SFTP error code mapping.
- `tests/unit/test_manager_*.py` — async manager with mocked transport: required/optional failure paths, fetch-files integration, pkey acceptance.
- `tests/unit/test_cli_*.py` — argparse, JSON-stdin parse, exit-code mapping.
- `tests/unit/test_daemon_ipc.py` — `spawn_daemon` end-to-end with a real worker subprocess; identity verification via the lockfile; regression test for leading-`-` tokens.
- `tests/unit/test_identity.py` — 5 cases covering `match`, `mismatch`, `not_found` (no PID), `not_found` (no file), `not_found` (stale file with no flock holder).
- `tests/unit/test_output_schema.py`, `test_exceptions.py` — output models, error serialization.

### 6.2 Integration (22 tests, all `@pytest.mark.integration`)

Docker compose topology (`tests/integration/docker-compose.yml`):

- `sshd-a`, `sshd-b`, `sshd-c` — three SSH servers on the default compose network with `/tmp/garuda-tunnel-it/` bind-mounted at `/srv/files`.
- `sshd-bastion` — SSH server attached to a separate `internal` network with `AllowTcpForwarding yes` enabled via a drop-in `sshd_config` snippet. Exposed on the host.
- `http-target-1`, `http-target-2` — `python:3.12-alpine` containers running a `ThreadingHTTPServer` that returns a unique `IDENTITY` env var on `GET /`. Attached only to `internal`; not reachable from the host directly.

Scenarios:

- `test_start.py` — happy path, required failure cleanup, optional failure warning, schema validation rejection.
- `test_multiport.py` — two handles to the same remote port get distinct local ports.
- `test_fetch_files.py` (8 cases) — single/multi file round-trip, ENOENT required/optional, perm denied, over-cap, mixed.
- `test_fetch_security.py` — `daemon.log_file` never receives fetched plaintext or its base64.
- `test_stop.py`, `test_status.py` — CLI lifecycle including wrong-token → `not_found` (via flock identity check).
- `test_remote_targets.py` (3 cases) — cross-host bastion forward identifies the target by HTTP body, multiple sequential requests through one forward, unreachable target fails at HTTP layer (SSH local forwards bind lazily; remote unreachability surfaces at first connect).

### 6.3 Gates

- `.venv/bin/black --check .`
- `.venv/bin/ruff format --check .`
- `.venv/bin/ruff check .`
- `.venv/bin/pylint garuda_tunnel/` → 10.00/10.
- `.venv/bin/vulture garuda_tunnel/ vulture_whitelist.py`
- `.venv/bin/mypy --strict garuda_tunnel/`
- Combined coverage via subprocess instrumentation (`sitecustomize.py` + `COVERAGE_PROCESS_START`): unit + integration combine → `coverage report --fail-under=80`. Current: 87%.

## 7. README

Audience: infrastructure engineers running short-lived jobs (CI runners, local containers, Terragrunt hooks) that need SSH-tunneled access plus a kubeconfig or similar config file from one or more remote hosts. Tool is generic — does not depend on Kubernetes — but the motivating use case is k3s edge nodes whose apiserver binds to `127.0.0.1`.

Sections in order: Title + tagline → Why this exists → Install (`pipx run --spec git+...@<NEXT_TAG>`) → End-to-end bash example → Input reference table → Output reference (JSON) → Error reference → Security notes → Troubleshooting table → Migration block (from the prior release) → Running tests → License.

## 8. Breaking changes (vs. prior release)

1. Input: `require: "*" | list[str]` → per-node `required: bool` on each `NodeInput`.
2. Input: `remote_ports: list[int]` (+ optional `local_ports: list[int]`) → `remote_targets: dict[str, "host:port"]`.
3. Input: `ssh_options.host_key_policy`, `ssh_options.known_hosts_path`, `ssh_options.threaded` removed (unused after the asyncssh migration; `extra=forbid` rejects them).
4. Output: `connections[node]: list[ConnectionEntry]` → `NodeOutput{ports: dict[str,int], fetch_files: dict[str, FetchedFile]}`.
5. Internal: `GARUDA_TUNNEL_TOKEN` env var is gone (replaced by lockfile-based identity). User-visible only insofar as `ps eww <daemon_pid>` no longer exposes the token.

The README "Migration" section gives concrete `jq` diff snippets for each.

## 9. Decisions and considerations

Key decisions made during design, with the alternatives that were rejected:

### 9.1 `remote_targets` shape

Considered:
- **A (chosen):** `dict[str, "host:port"]` — handle-keyed. Output mirrors handles. `jq '.connections.edge1.ports.kubeapi'` reads naturally.
- B: `list[{host, port}]` — verbose, output indexed by position, brittle for automation.
- C: `list[str]` (`"host:port"`) — compact but loses naming; same brittleness as B.
- D: keep `remote_ports: list[int]` + add `remote_host: str = "127.0.0.1"` per node — doesn't cover the "one bastion, multiple targets on different hosts" use case.

Chose A for symmetry with `fetch_files: dict[str, FileSpec]` and for jq-friendliness.

### 9.2 `remote_targets` backwards compatibility

Considered:
- **A (chosen):** clean break — `extra=forbid` rejects the old `remote_ports`/`local_ports` shape with a clear error. PR has not yet shipped; combine all breaking changes in one release.
- B: union — accept both list and dict forms. Doubles validator complexity for a temporary migration aid that no one needs (no public users yet).
- C: union + deprecation warning in stderr — same complexity as B without much benefit.

### 9.3 IPv6 parsing in `host:port`

Considered:
- **A (chosen):** require bracketed form `[ipv6]:port`. Unambiguous, matches RFC 3986 URI syntax.
- B: heuristic — try IPv6 split first, fall back to `rsplit(":", 1)`. Ambiguous for some addresses; bug-prone.

### 9.4 `local_ports` removal

Considered:
- **A (chosen):** remove entirely. OS picks. Simpler contract.
- B: keep, default to `None` (= OS picks), allow pinning. No current consumer asks for pinning.

YAGNI.

### 9.5 Cross-host integration test design

The new `test_remote_targets.py` needed to *prove* forwards reach the right container in an isolated network, not just that local listeners bind.

Considered:
- **A (chosen):** HTTP servers with `IDENTITY` env vars; test does `urllib.request.urlopen` and asserts on response body. Real HTTP, supports multiple sequential requests, identity verification is unambiguous.
- B: TCP echo via `nc -l -k -p N` — failed: busybox `nc -l -k` does not work correctly when stdin is from a pipe (closes after first connection).
- C: TCP server that sends identity eagerly then closes — would conflict with HTTP-style multiple requests requirement.
- D: SSH server on each target with different keys — overkill; same fundamental issue as A but with more cargo.

### 9.6 SSH local forward "unreachable target" detection

Considered:
- **A (chosen):** accept asyncssh's lazy bind semantics. `forward_local_port` always succeeds (local bind is independent of remote reachability); failure surfaces at first connect through the channel. Integration test for unreachable target asserts on HTTP-layer error, not on `start` exit code.
- B: add a "data probe" — after `forward_local_port`, connect to the local listener and try to read a byte. EOF/RST means remote unreachable. Rejected: false positives for silent servers (e.g. HTTP server that waits for request); would break existing tests that forward to ports where no service listens; semantic change without clear value.

### 9.7 Identity check: `fcntl.flock` vs alternatives

Considered:
- **A (chosen):** PID file + `fcntl.flock` on `~/.local/state/garuda-tunnel/<token>.lock`. Atomic, race-free, no `/proc` parsing, no environ leak. POSIX-only (Windows out of scope).
- B: Unix domain socket — daemon listens on `<token>.sock`; client connects + sends commands. Reframes the IPC entirely; not needed for the verification problem alone.
- C: `psutil` + environ — keeps the same race and same secrets-leak via `ps`. Adds a dependency for no improvement.
- D: keep environ parsing, add retry — fights the symptom not the cause.

### 9.8 Token transport: env vs argv vs pipe

Considered:
- **A (chosen):** worker argv `--token=<value>`. Equals form is critical: `["--token", value]` form lets argparse misinterpret a leading-`-` value as a flag (`secrets.token_urlsafe` returns base64url which starts with `-` ~1.7 % of the time). `--key=value` form is never split.
- B: env var `GARUDA_TUNNEL_TOKEN` (old approach) — leaks token to `ps eww`.
- C: dedicated pipe — adds another fd to inherit; the flock authority is the actual capability now, so the token in argv is just an opaque file-name lookup. argv is fine.

### 9.9 IPC mechanism

Considered:
- **A (chosen):** `subprocess.Popen(start_new_session=True, pass_fds=[ipc_write_fd], stdin=PIPE)`. Stdlib. Single API call replaces hand-rolled `os.fork()` + `os.setsid()` + second `os.fork()` + `os.execve()` + `os.set_inheritable()`. The IPC fd is dedicated and isolated from stdout/stderr — no `print`/logger collision risk.
- B: `multiprocessing.Process` + `Queue`/`Pipe` — optimized for shared-lifetime worker pools; daemon=True kills child on parent exit (opposite of what we need); spawn semantics on macOS lose shared state; atexit-cleanup fights detachment.
- C: `python-daemon` (PEP 3143) — handles daemonization but not IPC; we'd still need to add the response channel.
- D: keep hand-rolled fork + execve, debug the flake — every CI run consumes a roll of dice. Refactor pays for itself in stability.
- E: protocol via stdout — needs paranoia about library output. With a dedicated fd, this concern disappears.

### 9.10 stdout/stderr policy in the worker

Considered:
- **A (chosen):** parent controls via Popen's `stdout=`/`stderr=` arguments. `log_target` is either the user's `daemon.log_file` (open in append mode) or `subprocess.DEVNULL`. Worker does not touch its own standard fds; library log output goes wherever the parent pointed.
- B: worker `_redirect_standard_fds` to log file or `/dev/null` early in `_run` — what the previous implementation did. Hides startup errors from the user's terminal because the redirect happens before any work; also requires the worker to know about the parent's intent.

### 9.11 Stale lockfile cleanup

Considered:
- **A (chosen):** opportunistic sweep at the top of `spawn_daemon`. Tries `LOCK_NB` on every `*.lock`; unlinks any file that acquires (no live holder). Race-safe against concurrent daemons by construction.
- B: separate `garuda-tunnel gc` command — extra surface; the sweep at start is sufficient for the disposable-CI workload.
- C: cron / systemd timer — wrong layer; this is an application concern.

## 10. Constraints and invariants

- **One auth per node.** SFTP rides the existing `SSHClientConnection`. No second `asyncssh.connect()` inside `_start_one`.
- **One process per daemon.** Per-node concurrency via `asyncio.gather`, not subprocesses or threads.
- **No disk writes of secrets.** Private keys parsed in-memory via `asyncssh.import_private_key(pem)`. `fetch_files` output goes only to stdout, never to a tempfile.
- **1 MiB hard cap on every fetched file.** No tunable, no override.
- **Listener bind address always `127.0.0.1`.** `0.0.0.0` is never exposed.
- **`fetch_files` content is one-shot at `start`.** No daemon IPC verb for re-fetch.
- **Identity authority is `fcntl.flock` ownership.** The token is just an opaque file-name lookup; possession alone does not authenticate.
- **Worker daemon is detached after `start` returns.** Parent CLI exits; daemon reparents to init and lives until `stop` (or SIGTERM).

## 11. References

- Original tunnel spec: `docs/specs/2026-05-16-garuda-tunnel-design.md` (historical).
- asyncssh API: <https://asyncssh.readthedocs.io/en/stable/api.html>
- POSIX `flock`: `man 2 flock`
- `subprocess.Popen(start_new_session=...)`: <https://docs.python.org/3/library/subprocess.html#subprocess.Popen>
