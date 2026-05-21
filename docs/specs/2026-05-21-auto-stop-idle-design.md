# Auto-shutdown on idle: design

**Status:** draft
**Branch:** `feature/auto-stop-idle`
**Date:** 2026-05-21

## 1. Problem

`garuda-tunnel start` daemonizes and stays alive until the user runs `garuda-tunnel stop --pid X --token Y`. In disposable contexts (CI runners, ephemeral developer shells, container init scripts) the user may forget to call `stop`, or the parent process may exit abnormally before reaching the `stop` step. Result: orphaned daemons holding SSH connections to internal infrastructure, accumulating across runs.

We want the daemon to detect that no one is using its forwards and shut itself down after a configurable idle period.

## 2. Goals

- Optional, opt-in via a single `daemon.auto_stop_idle_seconds` integer in the input schema. Null (default) preserves today's "live until SIGTERM" behavior.
- "Idle" = zero active TCP connections through any forward, and the last connection lifecycle event (open or close) was more than N seconds ago.
- Timer starts at daemon ready time, not at first connect. A daemon that no one ever connects to also self-terminates after N seconds.
- Active long-running connections (e.g. `kubectl exec`, websockets) are detected and prevent the timer from firing. The semantic is "at least one live connection" — not "data flowed recently".
- No new dependencies beyond what we already have, except a patched fork of `asyncssh` that exposes a lifecycle hook (`ForwardTracker` Protocol).
- Upstream the asyncssh patch to `ronf/asyncssh`; once merged, drop the fork dependency.
- Update README install instructions to lead with `uvx` (more ergonomic for disposable use than `pipx`).

## 3. Non-goals

- Per-node or per-handle idle policy. Global daemon timer only.
- Byte-level activity (e.g. "X bytes/sec on any forward"). Connection-lifecycle is enough; tracking byte counts requires deeper instrumentation and is YAGNI for the disposable-CI use case.
- "Reset on heartbeat from client" — there is no client-facing IPC protocol; the daemon observes only what flows through asyncssh.
- Renew/extend timer at runtime. Schema is fixed at `start`; reconfiguration would need a control plane that doesn't exist.
- Notify the parent or stdout when auto-shutdown fires. The CLI parent has long since exited.

## 4. asyncssh fork: `ForwardTracker` Protocol

### 4.1 New public API in `asyncssh`

Add to `asyncssh/forward.py`:

```python
from typing import Protocol


class ForwardTracker(Protocol):
    """Optional hooks for observing forward connection lifecycle.

    Each method is called from within the asyncio loop. Implementations
    MUST NOT block (no I/O, no sleep). All hooks are best-effort —
    asyncssh swallows any exception they raise.
    """

    def connection_made(self, orig_host: str, orig_port: int) -> None:
        """A new client TCP connection was accepted on the local listener."""

    def connection_lost(
        self,
        orig_host: str,
        orig_port: int,
        exc: Exception | None,
    ) -> None:
        """A previously-accepted connection has closed (clean exc=None or
        with an error)."""
```

Re-export from `asyncssh/__init__.py`.

`SSHClientConnection.forward_local_port` gains a new optional kwarg:

```python
async def forward_local_port(
    self, listen_host, listen_port, dest_host, dest_port,
    accept_handler: SSHAcceptHandler | None = None,
    tracker: ForwardTracker | None = None,
) -> SSHListener: ...
```

`tracker` is threaded through `create_tcp_forward_listener` and stored on `SSHLocalPortForwarder`. The forwarder calls `tracker.connection_made(...)` from inside its own `connection_made(transport)` hook (after `super().connection_made(transport)` succeeds), and `tracker.connection_lost(...)` from inside its own `connection_lost(exc)` hook. Both calls are guarded by `try/except Exception: logger.debug(...)` so a buggy tracker cannot break the forwarder.

### 4.2 Backwards compatibility

`tracker` defaults to `None`. When `None`, the behavior of `SSHLocalPortForwarder` is identical to today. No existing call site needs to change. The Protocol approach (PEP 544) means user code does not need to subclass anything; any object with the two methods satisfies it.

### 4.3 Fork management

- Fork repository: `https://github.com/AlexMKX/asyncssh`.
- Branch: `feat/forward-tracker`.
- Tag: `2.23.0+forward-tracker.1` (PEP 440 local version identifier; safe for `pip install`).
- Upstream PR will be filed by the project owner against `ronf:develop` (or `main`, whichever asyncssh uses).
- Until upstream merges:
  - `pyproject.toml` of `garuda-tunnel` depends on the fork via VCS URL.
  - We monitor upstream for asyncssh bugfixes that we may want to cherry-pick.
- When upstream merges and ships (asyncssh ≥ 2.24 or 3.0):
  - Switch `pyproject.toml` back to `asyncssh>={whatever},<3` (or appropriate range).
  - Tag the fork as archived; close.

### 4.4 What lands in the fork PR

| File | Change |
|---|---|
| `asyncssh/forward.py` | New `ForwardTracker` Protocol; `SSHLocalPortForwarder.__init__` accepts optional tracker; `connection_made`/`connection_lost` call hooks with try/except. |
| `asyncssh/connection.py` | `SSHClientConnection.forward_local_port` accepts `tracker` kwarg; passes through `create_tcp_forward_listener`. |
| `asyncssh/listener.py` | `create_tcp_forward_listener` accepts and threads `tracker` into `protocol_factory`. |
| `asyncssh/__init__.py` | Re-export `ForwardTracker`. |
| `docs/api.rst` | New section "Forward connection tracking" with a short example. |
| `tests/test_forward.py` | Two new tests: hook fires on accept, hook fires on close (clean and on-exception). |
| `CHANGELOG.rst` | Entry describing the new feature. |

## 5. Schema

```python
# garuda_tunnel/schemas.py

class DaemonOptions(BaseModel):
    """Daemon-side knobs: log file, shutdown grace, and idle stop."""

    model_config = ConfigDict(extra="forbid")

    log_file: str | None = None
    shutdown_grace_seconds: int = 10
    auto_stop_idle_seconds: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Auto-shutdown timeout in seconds. If set, the daemon sends "
            "itself SIGTERM when no tunnel forward has had an active "
            "connection for this many seconds. Timer starts when the "
            "daemon comes up; any open or close of a forward connection "
            "resets it. Active long-lived connections prevent shutdown. "
            "Null (default) disables auto-shutdown."
        ),
    )
```

Validation:
- `None` = disabled (default).
- `int >= 1` accepted.
- `0`, negative, or non-int → `ValidationError`.
- `extra="forbid"` rejects typos.

## 6. Components

### 6.1 `garuda_tunnel/activity.py` (new file)

```python
"""Forward-connection activity tracker for idle-based auto-stop."""

from __future__ import annotations

import time


class ActivityTracker:
    """Single counter + last-activity timestamp shared across all forwards.

    Implements asyncssh.ForwardTracker Protocol (duck-typed). Aggregates
    open/close events across every node and handle into one daemon-wide
    signal: "is any forward currently in use?" + "when did the last
    lifecycle event happen?"
    """

    def __init__(self) -> None:
        self._active_count = 0
        self._last_activity_at = time.monotonic()

    def connection_made(self, orig_host: str, orig_port: int) -> None:
        """asyncssh hook: a new client TCP connection was accepted."""
        del orig_host, orig_port  # Aggregate-only; per-conn detail unused.
        self._active_count += 1
        self._last_activity_at = time.monotonic()

    def connection_lost(
        self, orig_host: str, orig_port: int, exc: Exception | None
    ) -> None:
        """asyncssh hook: a previously-accepted connection has closed."""
        del orig_host, orig_port, exc
        self._active_count = max(0, self._active_count - 1)
        self._last_activity_at = time.monotonic()

    @property
    def is_idle(self) -> bool:
        """True iff there are zero active forward connections right now."""
        return self._active_count == 0

    @property
    def seconds_since_activity(self) -> float:
        """Wall-clock seconds since the most recent open/close event."""
        return time.monotonic() - self._last_activity_at
```

Thread/concurrency notes: all calls happen on the single asyncio loop thread. No locks needed. Underflow guard (`max(0, ...)`) covers theoretical races during shutdown when listeners close before forwarders finalize.

### 6.2 `garuda_tunnel/_worker.py` — idle watchdog

```python
async def _idle_watchdog(
    tracker: ActivityTracker,
    timeout_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    """Poll every timeout/4 seconds; set stop_event when idle past threshold.

    Cancellation-safe: returns cleanly on CancelledError so the cleanup
    finally-block in `_run` can await the task without raising.
    """
    poll_interval = max(1.0, timeout_seconds / 4)
    while not stop_event.is_set():
        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return
        if tracker.is_idle and tracker.seconds_since_activity >= timeout_seconds:
            stop_event.set()
            return
```

Integration into `_run` after IPC response is sent and before `await stop_event.wait()`:

```python
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
    _release_identity_lock(lock_fd, args.token)
```

### 6.3 `garuda_tunnel/manager.py` — tracker ownership

```python
class TunnelManager:
    def __init__(self, schema: InputSchema) -> None:
        self.schema = schema
        self.activity_tracker = ActivityTracker()
        # ... existing fields ...
```

The tracker is owned by the manager (one per daemon) and passed down to `open_local_forwards`.

### 6.4 `garuda_tunnel/ssh.py` — wire the tracker

```python
async def open_local_forwards(
    conn: asyncssh.SSHClientConnection,
    node: NodeInput,
    tracker: asyncssh.ForwardTracker | None = None,
) -> tuple[dict[str, int], list[asyncssh.SSHListener]]:
    """Open one direct-tcpip forward per remote_target.

    If ``tracker`` is provided, asyncssh will invoke its hooks on every
    client connect/disconnect on the local listener. Used by the daemon
    for idle-based auto-shutdown.
    """
    ports: dict[str, int] = {}
    listeners: list[asyncssh.SSHListener] = []
    timeout = float(node.ssh_options.connect_timeout)

    try:
        for handle, target in node.remote_targets.items():
            listener = await conn.forward_local_port(
                "127.0.0.1", 0, target.host, target.port,
                tracker=tracker,
            )
            # ... probe and bookkeeping unchanged ...
    # ... cleanup unchanged ...
```

`open_local_forwards` keeps backwards-compat: callers that don't pass `tracker` see no change.

`manager._start_one` (or wherever `open_local_forwards` is called) passes `tracker=self.activity_tracker`.

## 7. README — install section

Replace the current Install section. Final:

````markdown
## Install

`uvx` (recommended for one-shot / disposable use — no install needed):

```bash
uvx --from git+https://github.com/AlexMKX/garuda-tunnel.git garuda-tunnel --help
```

`pipx` (persistent install):

```bash
pipx install git+https://github.com/AlexMKX/garuda-tunnel.git
```

For development:

```bash
git clone https://github.com/AlexMKX/garuda-tunnel.git && cd garuda-tunnel
pip install -e ".[dev]"
```

Requires Python >= 3.10. Linux and macOS supported; Windows works via WSL only.
````

The README's end-to-end example also gets a new top-level `daemon` block demonstrating `auto_stop_idle_seconds`:

```bash
JSON=$(cat <<EOF
{
  "nodes": {
    "edge1": {
      "host": "198.51.100.10",
      "user": "root",
      "ssh_pkey": $(jq -Rs . <<<"$PRIVATE_KEY"),
      "remote_targets": {
        "kubeapi": "127.0.0.1:6443"
      }
    }
  },
  "daemon": {
    "auto_stop_idle_seconds": 600
  }
}
EOF
)
```

With a short prose note: "If no client connects for 10 minutes, the daemon shuts down on its own. Useful for ephemeral CI runs that may abort before reaching `garuda-tunnel stop`."

## 8. Tests

### 8.1 Unit (new files)

**`tests/unit/test_activity.py`**:
- `test_initial_state`: fresh tracker is idle, `seconds_since_activity` small.
- `test_connection_made_increments`: one `connection_made` → not idle.
- `test_connection_lost_decrements`: `made + lost` → idle.
- `test_multiple_concurrent_connections`: 3 `made` + 1 `lost` → not idle, active_count == 2.
- `test_underflow_protected`: `lost` without prior `made` → active_count stays 0.
- `test_last_activity_updates_on_both`: both events bump `last_activity_at`.

**`tests/unit/test_idle_watchdog.py`**:
- `test_no_activity_triggers_stop`: empty tracker, `timeout=0.1` → `stop_event` set within 0.5s.
- `test_activity_resets_timer`: `connection_made` mid-poll → `stop_event` not set within timeout window.
- `test_active_connection_blocks_stop`: `made` without `lost`, wait 2x timeout → `stop_event` not set.
- `test_lost_after_idle_period_triggers_stop`: `made + lost`, wait N → `stop_event` set.
- `test_cancellation_clean`: `task.cancel()` → no exception, task ends.

**`tests/unit/test_schemas.py`** (additions):
- `test_auto_stop_idle_seconds_default_null`: `DaemonOptions().auto_stop_idle_seconds is None`.
- `test_auto_stop_idle_seconds_accepts_int`: `DaemonOptions(auto_stop_idle_seconds=60).auto_stop_idle_seconds == 60`.
- `test_auto_stop_idle_seconds_rejects_zero`: `DaemonOptions(auto_stop_idle_seconds=0)` → `ValidationError`.
- `test_auto_stop_idle_seconds_rejects_negative`: `DaemonOptions(auto_stop_idle_seconds=-1)` → `ValidationError`.

### 8.2 Integration (`tests/integration/test_auto_stop.py`, new file)

**`test_idle_auto_stop_kills_daemon`**:
1. `garuda-tunnel start` with payload `{..., "daemon": {"auto_stop_idle_seconds": 3}}`.
2. Do not connect to any forward.
3. Sleep 5 seconds.
4. `os.kill(pid, 0)` raises `ProcessLookupError` (daemon exited).
5. `<state_dir>/<token>.lock` no longer exists (graceful shutdown ran `_release_identity_lock`).

**`test_active_connection_prevents_auto_stop`** (optional, may be moved to a follow-up if integration runtime grows):
1. Start daemon with `auto_stop_idle_seconds=3`, one HTTP target.
2. Open a TCP socket to the local listener and hold it open.
3. Sleep 5 seconds. `os.kill(pid, 0)` succeeds (daemon alive).
4. Close socket. Sleep 5 seconds. `os.kill(pid, 0)` raises (daemon exited).

### 8.3 Existing tests

No existing test should break. The `DaemonOptions` change is additive (new optional field with safe default). The `ssh.open_local_forwards` change is additive (new optional kwarg). Existing test fixtures call neither.

## 9. Lifecycle: exit conditions

After this change, the worker exits cleanly when any of these happen (whichever first):
- `SIGTERM` / `SIGINT` from outside (existing behavior).
- `stop_event` set by `_idle_watchdog` because `is_idle and seconds_since_activity >= N` (new).
- Required-node failure during `start` (existing; not affected).
- Schema parse error or lock-acquire error (existing; not affected).

In all paths, `_release_identity_lock` runs in the finally block of `_run`. The lockfile is removed; subsequent `garuda-tunnel status --pid X --token Y` will report `not_found`.

## 10. Decisions and considerations

### 10.1 Connection lifecycle vs byte tracking

Chose: connection-lifecycle (open/close) tracking via `ForwardTracker`.

Alternatives considered:
- **Byte counters via custom `SSHForwarder` subclass.** Catches long-running idle channels (e.g. websocket where no data flows but the connection is alive). Overkill for our disposable-CI use case where everything looks like short HTTP request/response. Could be added later by extending `ForwardTracker` with `bytes_in` / `bytes_out` methods — Protocol class is forward-compatible.
- **Open-only via `accept_handler`.** No close signal. Cannot answer "are there active connections right now?", so long-running clients would incorrectly trigger shutdown. Rejected.

### 10.2 asyncssh patch path

Chose: fork + upstream PR.

Alternatives considered:
- **Private-API workaround** (subclass `SSHLocalPortForwarder` directly). No upstream value, fragile across asyncssh releases (private API can change in any minor bump). We'd own the workaround forever.
- **`asyncio.start_server` + manual bidirectional copy.** Reimplements 50+ lines of asyncssh's forwarding code. Loses asyncssh's correctness guarantees around channel teardown, EOF propagation, half-close. Higher maintenance burden.
- **Wait for upstream feature without using it.** Blocks our feature on someone else's review queue indefinitely.

Fork+upstream is the pattern that:
- Gets us a working feature now.
- Contributes value back to all asyncssh users.
- Has a clear retirement path (drop the fork dep once upstream merges).

### 10.3 ForwardTracker shape: Protocol vs callables vs class

Chose: `typing.Protocol` with two methods.

Alternatives considered:
- **Two callable params `on_connection_made`, `on_connection_lost`.** Simpler signature but harder to extend (every new hook is another kwarg). Less idiomatic for asyncio code (compare `asyncio.BaseProtocol`).
- **Subclassable abstract base class.** Requires user code to inherit. Protocol (PEP 544) lets a plain class with the right methods satisfy the type, which is more Pythonic.

### 10.4 Idle scope: per-daemon vs per-node vs per-handle

Chose: per-daemon.

Alternatives considered:
- **Per-node:** node idle → stop just that node's forwards. Then `garuda-tunnel status` becomes complicated ("partial daemon"); `garuda-tunnel stop` semantics unclear. Worth doing if real demand emerges; YAGNI now.
- **Per-handle:** even finer granularity. Same issues as per-node, more bookkeeping.

### 10.5 Timer start: at daemon ready vs at first connect

Chose: at daemon ready.

Alternatives considered:
- **At first connect.** A daemon nobody touches stays alive forever. Defeats the "abandoned start" protection.
- **Hybrid (configurable `grace_seconds`).** Adds a second field for marginal benefit. YAGNI.

### 10.6 Poll interval

Chose: `max(1.0, timeout / 4)`.

This guarantees:
- Sub-second resolution is bounded (no busy loop for `timeout=10` we'd poll at 2.5s, fine).
- Very long timeouts (e.g. 3600s) poll at 900s — daemon will be at most 15 minutes late to shut down, but that's negligible vs the 1-hour budget.
- Floor of 1 second prevents pathological short polls if user sets very small timeouts.

### 10.7 Hook exception handling

Chose: asyncssh swallows exceptions raised by `ForwardTracker` hooks (log at debug level).

Alternative: propagate exceptions. Would let a buggy tracker close the channel mid-transfer. Tracker is an observer, not a gatekeeper — `accept_handler` is the gatekeeper for that.

### 10.8 README install: uvx vs pipx

Chose: `uvx` first, `pipx` second.

`uvx` does not install anything globally; it runs the binary from a fresh, cached venv. For a tool that's invoked maybe once per CI job and never again, this is strictly better than `pipx`'s persistent install. `pipx` stays as the option for developers who run `garuda-tunnel` interactively day-to-day.

## 11. Constraints and invariants

- Tracker is per-daemon (one instance), shared across all nodes and handles.
- `auto_stop_idle_seconds=None` means tracker still runs (asyncssh calls its hooks) but watchdog task is not created — zero overhead beyond a single counter increment per connect/disconnect.
- Watchdog runs in the same asyncio loop as everything else; no extra thread.
- Tracker state is monotonic-clock based; immune to system clock adjustments.
- The daemon never reaches out to the parent CLI to report auto-shutdown; the parent is gone by then. Stale lockfile cleanup happens at the next `start` via `_sweep_stale_lockfiles`.

## 12. Out of scope (deferred)

- Per-node / per-handle idle timeouts.
- Byte-level activity tracking.
- A `garuda-tunnel keepalive --pid X --token Y` command for clients to reset the timer without opening a real TCP connection.
- A "minimum lifetime" floor (don't auto-stop in the first 60 seconds regardless).
- Notifying the user / log line / event when auto-stop fires (the daemon's stderr goes to `log_file` if set; we can add a log line in a follow-up if it turns out to be hard to debug).

## 13. References

- asyncssh `forward_local_port`: <https://asyncssh.readthedocs.io/en/stable/api.html#asyncssh.SSHClientConnection.forward_local_port>
- PEP 544 (Protocols): <https://peps.python.org/pep-0544/>
- PEP 440 local version identifiers: <https://peps.python.org/pep-0440/#local-version-identifiers>
- `uvx` docs: <https://docs.astral.sh/uv/guides/tools/>
