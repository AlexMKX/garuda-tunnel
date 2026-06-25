# Deterministic session reuse + single path-keyed lock (issue #7)

- Status: design, awaiting review
- Date: 2026-06-24
- Issue: [#7 Check that session path is auto-cleaned if no session active](https://github.com/AlexMKX/tunstrap/issues/7)
- Scope: Task A of the CLI roadmap (precedes #6 CLI mode and #5 run wrapper)

## Problem

Some callers prefer a deterministic `--session-dir` and pass the same path on
every invocation. Today `start` on a path whose `tunnel-data/` already exists
**always** fails:

```
session.py:_validate_data_slot
    raise SessionError("tunnel-data already exists (possible orphaned session);
                        remove it before reusing this session dir")
```

This is wrong when the previous daemon has already exited — the leftover
`tunnel-data/` is orphaned and should simply be reclaimed. The tool must fail
**only** when a session is genuinely *active* on that path.

The operative definition of "active", per the issue owner: **a daemon is
currently running (or concurrently starting) on the same session path.** This
is a mutual-exclusion problem, not a liveness *heuristic* — the fix must be
race-free against two `start` invocations on the same path at the same instant.

## Current state (as-is)

- Each `start` mints a random `token` (`daemon.py:75`,
  `secrets.token_urlsafe(32)`), passes it to the worker via `--token`.
- The worker acquires `flock(LOCK_EX|LOCK_NB)` on a **per-token** lockfile
  `<session_dir>/tunnel-data/<token>.lock` (`_worker.py:_acquire_identity_lock`,
  called with `data_dir = session_dir/tunnel-data`). Lock content = pid.
- `write_identity()` writes `daemon.pid` and `token` (0600) into `tunnel-data/`.
- `stop`/`status` read `(pid, token)` and call `verify_token(pid, token,
  state_dir=tunnel-data)` → `match` / `mismatch` / `not_found` / `unavailable`.
- `OutputSchema.token` is returned to the caller as a capability for
  `stop`/`status`.
- `daemon._sweep_stale_lockfiles()` globs `_state_dir()` (`~/.local/state/
  tunstrap/*.lock`) — but the real locks live in `tunnel-data/`, so the sweeper
  currently matches nothing (latent dead code, removed below).

Two per-token lockfiles for two daemons on the same path do **not** mutually
exclude each other, so the current model cannot detect concurrent reuse anyway.

## Design (to-be)

### One lock, in the session dir itself

Replace the per-token lock with a **single** lock per session, living in the
session dir at root, beside `tunnel-data/`:

```
<session_dir>/session.lock
```

- The session path is supplied by the caller and is already the stable
  identity, so the lock just lives there — no hashing, no separate state dir.
  Two daemons on the same path open the same file and contend on one flock.
- Sits **beside** `tunnel-data/`, not inside it, so `tunnel-data/` can be wiped
  and recreated while the lock is held.
- Content = `<pid>\n` (used by `stop`/`status` for the pid, and to confirm
  identity).
- Applied uniformly to **both** supplied and generated session dirs (a
  generated `mkdtemp` path is unique, so its lock can never contend — uniform
  handling, no special case).

This retires the `~/.local/state/tunstrap` machinery entirely:
`identity._state_dir()` and `daemon._sweep_stale_lockfiles()` become dead code
and are removed. There is nothing to sweep — a stale `session.lock` on a
supplied path is simply re-acquired on the next reuse (flock is free →
reclaim); a generated dir is removed wholesale on cleanup.

### Remove `token` entirely

With a path-keyed lock, the session path *is* the identity and the capability.
`token` loses every role and is removed:

- Drop `token` from `OutputSchema`, IPC payloads, and
  `start_all_and_build_output(...)`.
- Drop `--token` from the `_worker` argparser and from `daemon.spawn_daemon`
  (no more `secrets.token_urlsafe`).
- Drop `--token` from `status`; `status` and `stop` key off `--session-dir`.
- `write_identity(pid=...)` writes only `daemon.pid`; the `token` file is gone.
- `read_identity()` returns `pid` only.
- `identity.verify_token(pid, token, state_dir)` →
  `verify_session(session_dir, pid)`: open `<session_dir>/session.lock`,
  flock-probe, compare the recorded pid. Returns the same `IdentityCheckResult`
  enum (`match`/`mismatch`/`not_found`/`unavailable`).

This is a breaking change to `OutputSchema` (callers reading `.token` break);
acceptable and intended for this pre-1.0 tool.

### Worker startup flow (reuse + exclusion)

In `_worker.main()` / `_run()`:

1. Resolve session dir (`SessionDir.create` still validates absolute-path and
   the `tunnel-data` safety invariants below).
2. Acquire the single lock: open `<session_dir>/session.lock`,
   `flock(LOCK_EX | LOCK_NB)`.
   - **`BlockingIOError`** → a daemon is active on this path. Report IPC
     `{"kind": "session_active", ...}`; the worker exits without touching
     `tunnel-data/`.
   - **acquired** → keep the fd for the worker lifetime; write `<pid>` into the
     lock file.

   (`session.lock` must be created before/around the `tunnel-data/` reclaim, and
   the safety checks below still apply to `tunnel-data/`. The lock file itself is
   never wiped by reclaim.)
3. Reclaim `tunnel-data/`: if it exists and passes the safety checks (see
   below), wipe and recreate it fresh (mode 0700); otherwise create it. Because
   we hold the exclusive path-lock, any pre-existing `tunnel-data/` is provably
   orphaned.
4. `write_identity(pid=...)`, run the tunnel manager as today.
5. On exit (`finally`/clean or signal), release + unlink the path-lock (kernel
   also releases flock on process death).

`SessionDir` safety invariants are **preserved** as hard errors (untrusted
`--session-dir`): `tunnel-data` that is a symlink, a non-directory, or owned by
another uid is still rejected — never silently wiped.

### CLI / exit-code surface

- New error `SessionActive` → exit code **3** (free; does not collide with
  existing `1` schema, `2` required/kube, `4` daemon, `64` usage, and leaves the
  child-exit-code range for the future #5 `run` wrapper untouched).
- New IPC kind `session_active`; `start_command` maps it to exit 3 and prints
  the structured error (`"session already active at <path>"`), with no
  side effects on `tunnel-data/`.
- `status`: `--session-dir` becomes the primary input (derive lock + pid);
  `--pid`/`--token` removed.
- `stop`: unchanged signature except it no longer reads/needs `token`.

## Components touched

| File | Change |
|------|--------|
| `identity.py` | Add `acquire_session_lock(session_dir) -> fd` and `verify_session(session_dir, pid)` operating on `<session_dir>/session.lock`. Remove token-keyed `verify_token` and `_state_dir()`. |
| `_worker.py` | Acquire `session.lock` early; busy → IPC `session_active`; drop `--token`; `write_identity(pid=...)`; release lock on exit. |
| `session.py` | `_validate_data_slot` → reclaim semantics (wipe+recreate when held); keep symlink/non-dir/owner raises; `write_identity(pid)`; `read_identity` returns pid only. |
| `daemon.py` | Remove `runtime_token`/`--token`/`secrets` and the dead `_sweep_stale_lockfiles()`. |
| `cli.py` | `status` keys off `--session-dir` (drop `--pid`/`--token`); map `session_active` → exit 3; `stop` drops token. |
| `schemas.py` | Remove `token` from `OutputSchema`. |
| `exceptions.py` | Add `SessionActive` (exit 3); register IPC kind `session_active`. |

## Error handling

- Busy path-lock → exit 3, structured error, `tunnel-data/` untouched.
- Generated (temp) path → unique lock, never contends; unchanged UX.
- Orphaned `tunnel-data/` while we hold the lock → silently reclaimed.
- Unsafe pre-existing `tunnel-data/` (symlink/non-dir/foreign owner) → hard
  `SessionError` (unchanged).

## Testing (TDD)

Unit:
- `SessionDir`: pre-existing safe `tunnel-data/` + held lock → wipe+recreate;
  symlink / non-dir / foreign-owner → raises (preserved).
- `identity`: `verify_session` returns `match` when the lock is held by the
  recorded pid and `not_found` when free/dead.
- Concurrency: two `acquire_session_lock` on the same session dir → second
  raises `BlockingIOError` (second start → `session_active`).
- Regression: `OutputSchema` has no `token`; `status`/`stop` operate by
  `--session-dir`.

Integration:
- Deterministic path, first daemon alive → second `start` exits 3 and leaves the
  first session intact.
- `stop` first, then `start` same path → succeeds, stale `tunnel-data/` cleared.
- Orphaned `session.lock` (daemon killed) is reclaimed on the next `start`.

## Interaction with prior specs

`2026-05-21-auto-stop-idle-design.md` states that orphaned lockfiles are cleaned
at the next `start` via `_sweep_stale_lockfiles()`. That mechanism is removed
here; its role is superseded by session-local reclaim — a dead session's
`session.lock` is re-acquired (and `tunnel-data/` wiped) on the next `start`
against the same path. A graceful auto-stop still releases the lock in its
`finally` path exactly as before.

## Out of scope

- #6 (CLI input / `--output env`) and #5 (`run` wrapper). The `TUNSTRAP_*` env
  contract and child-process exit-code propagation are designed there; this
  spec only reserves exit code 3 so they do not collide.
