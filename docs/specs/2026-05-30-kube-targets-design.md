# Design: Kube-targets — self-contained kubeconfig forwarding and patching

**Date:** 2026-05-30
**Status:** Approved (brainstorming complete)
**Repo:** `garuda-tunnel`
**Branch:** `feature/kube-targets`

## Problem

Consumers of `garuda-tunnel` that talk to k3s/Kubernetes clusters over the
SSH tunnel must currently reconstruct a usable kubeconfig themselves. In the
Terragrunt/Terraform consumer (`garuda-repo/examples/mini-site/garuda/locals.tf`,
lines 169-245, ~75 lines of HCL) this means:

1. `jsondecode` the tunnel output,
2. `base64decode` + `yamldecode` the fetched raw kubeconfig,
3. project it to a minimal `{clusters, users}` shape to satisfy HCL static
   type unification,
4. **discard** the file's `server:` value entirely and rebuild the endpoint
   from the forwarded port (`https://127.0.0.1:${ports["k3s"]}`),
5. add placeholder branches for the empty/tftest state.

The raw kubeconfig is unusable as-is because:

- its `server:` points at the address the apiserver sees itself on (often
  `127.0.0.1:6443`, or an internal VPC IP, or a split-horizon DNS name) — not
  at the OS-assigned local forwarded port the client must use;
- after rewriting `server:` to `https://127.0.0.1:<local_port>`, TLS
  verification fails because the apiserver certificate's SAN does not include
  `127.0.0.1`. The correct `tls-server-name` **cannot be derived from the
  kubeconfig** — it lives in the apiserver's serving certificate SAN, which the
  kubeconfig does not contain.

The goal is to make `garuda-tunnel` produce a ready-to-use kubeconfig so the
HCL-side reconstruction disappears, while keeping the existing generic
byte-forwarding (`remote_targets`) and "content never to disk" guarantee
intact for current consumers.

## Goals

- A self-contained **kube mode**: given one kubeconfig path on the remote, the
  tunnel reads it, resolves the server host **on the SSH-server side**
  (split-horizon correct), opens the forward, probes the apiserver's
  certificate SAN to choose a `tls-server-name`, patches `server:` to the local
  forwarded port, and returns both a patched kubeconfig and the already-extracted
  fields (endpoint, CA, client cert/key, TLS server name).
- Optional **on-disk materialization** of fetched/patched files, off by default.
- A **session directory** with a well-known `tunnel-data/` subdirectory that the
  daemon owns and cleans up.
- Additive at the field level: `kube_targets`, `materialize`, and
  `--session-dir` add new fields/flags; no existing field is removed or renamed,
  and `remote_targets`/`fetch_files` are untouched. Consumers that ignore
  unknown fields are unaffected; strict consumers (e.g. pydantic `extra=forbid`)
  must account for the new always-present `session_dir` output field and the new
  per-node `kube_targets` output section. The legacy `stop --pid --token`
  invocation is **removed** in favor of `stop --session-dir` (see Decision log) —
  per `global-rules` we do not carry backward compatibility without an explicit
  instruction, and none was given.

## Non-goals (YAGNI)

- Startup sweep of orphaned session directories (see Known Limitations).
- `auto-unlink-on-close` (unlinked-but-open) file semantics — incompatible with
  external consumers needing a real path.
- Generic `transforms`/jsonpatch plugin framework — rejected in favor of the
  focused kube mode.
- Additional target types (e.g. `helm_targets`) — the design leaves room but
  does not implement them.
- Multi-cluster handling inside a single `kube_target` — see the one-cluster
  invariant below.

## Key invariants and decisions

- **One `kube_target` = exactly one cluster.** The tunnel reads the kubeconfig,
  takes its `current-context`, and from that context derives exactly one
  `cluster` + one `user`. Any other clusters/contexts present in the file are
  ignored and left untouched in the patched output. To access two clusters, use
  two `kube_targets` (with two kubeconfig paths) — possibly across two node
  connections. When the kubeconfig contains more than one context (or clusters
  other than the current-context cluster), the daemon emits a `warnings[]` entry
  naming the ignored contexts, so the operator is not silently surprised that
  only one cluster is reachable through the tunnel.
- **cluster↔user binding via `current-context`.** The `current-context` names
  both the cluster and the user; CA comes from the cluster, client cert/key from
  the user.
- **Server host resolution happens on the SSH-server side**, so split-horizon
  DNS (a hostname that resolves differently inside the server's network)
  resolves correctly.
- **TLS server name selection:** prefer the host from the original `server:`
  URL if it appears in the certificate SAN; else the first DNS-type SAN; else
  the first IP-type SAN. An explicit `KubeTarget.tls_server_name` overrides the
  probe entirely. **Whenever the chosen name is not an exact match of the
  original `server:` host** (i.e. a DNS/IP fallback was used), the daemon emits a
  `warnings[]` entry naming the chosen SAN, so silent selection of an unintended
  name is visible.
- **`insecure_fallback` is `false` by default** (secure by default). This is the
  only TLS-safety fallback in the design; per `global-rules` (`code.md:31-33`)
  its plan is fixed here upfront:
  - **Scope:** per `KubeTarget`, opt-in only.
  - **Trigger:** SAN probe yields no usable name *and* no explicit
    `tls_server_name` is given.
  - **Effect when `false` (default):** the target fails (subject to `required`)
    with a clear error — fail-fast.
  - **Effect when `true`:** the patcher emits `insecure-skip-tls-verify: true`,
    drops `certificate-authority-data`, and emits a `warnings[]` entry recording
    that verification was disabled for that target (observability).
  - **Retirement:** intended for disposable/CI use over the SSH tunnel; revisit
    and remove once host-key pinning + SAN-probe reliability make it unnecessary.
- **`materialize` is `false` by default**, preserving the existing
  "content never to disk" guarantee for current consumers.
- **YAML library: `ruamel.yaml`** (round-trip), preserving comments and key
  order in the patched kubeconfig. Loaded in round-trip/safe mode
  (`YAML(typ="rt")`) — no unsafe constructors. Version constraint
  `ruamel.yaml>=0.18,<0.19`; verified actively maintained as of 2026.
- **Session directory:** the daemon always works inside a `tunnel-data/`
  subdirectory of the session dir (a well-known name). Cleanup always removes
  `<session-dir>/tunnel-data` wholesale. If the daemon generated the session dir
  itself, it removes the whole generated dir; if the session dir was supplied by
  the caller, only `tunnel-data` is removed (the caller's directory is never
  touched).

## Schema changes

### Input

`NodeInput` gains an optional field alongside `remote_targets` and `fetch_files`:

```python
kube_targets: dict[str, KubeTarget] | None = None
```

Logical key constraint: `^[a-zA-Z_][a-zA-Z0-9_-]*$`, 1..64 chars (same as
`fetch_files`).

New model `KubeTarget`:

| Field | Type | Default | Description |
|---|---|---|---|
| `kubeconfig_path` | `str` | required | Absolute remote path. Same rules as `FileSpec.path` (starts with `/`, no `~`, no `$VAR`, ≤4096). |
| `tls_server_name` | `str \| null` | `null` | Explicit hint. If set, overrides SAN probe. |
| `insecure_fallback` | `bool` | `false` | If SAN probe yields no name and no `tls_server_name`: `true` → emit `insecure-skip-tls-verify`, drop CA, warn; `false` → target fails (subject to `required`). |
| `required` | `bool` | `true` | If `false`, this target's failure does not fail the node. |

`daemon` gains:

| Field | Type | Default | Description |
|---|---|---|---|
| `materialize` | `bool` | `false` | Write fetched/patched files to disk in the session dir. |

> `materialize` is placed at **`daemon` scope** (not per `KubeTarget`) on
> purpose: the session directory is a daemon-level resource, so the policy of
> whether *anything* is written into it is a single daemon-level switch rather
> than a per-target knob. There is no use case for materializing one target but
> not another within the same daemon.
>
> `daemon.runtime_dir` is **not** added. Session-dir location is controlled via
> the `start --session-dir` CLI flag (see CLI changes) so that the same path can
> be handed to `stop --session-dir`. The default location (when no flag is
> given) remains the current token directory.

### Output

`NodeOutput` gains a `kube_targets` section alongside `ports` and `fetch_files`
(named to match the `fetch_files` input→output precedent). Because of the
one-cluster invariant, each entry is flat (no nested clusters map):

```jsonc
"connections": {
  "hub": {
    "ports": { /* existing, from remote_targets */ },
    "fetch_files": { /* existing */ },
    "kube_targets": {
      "<kube_target_name>": {
        "cluster_name": "production",                  // from current-context
        "context_name": "production",                  // current-context name
        "local_port": 40123,
        "endpoint": "https://127.0.0.1:40123",
        "tls_server_name": "am.prod.kube.example.net", // chosen SAN / hint / null if insecure
        "certificate_authority_data": "<b64>",         // "" if insecure_fallback fired
        "client_certificate_data": "<b64>",
        "client_key_data": "<b64>",
        "content_b64": "...",                          // full patched kubeconfig (always)
        "path": "/abs/session/tunnel-data/hub-k3s"     // null unless daemon.materialize=true
      }
    }
  }
},
"pid": 12345,
"token": "<opaque>",
"session_dir": "/abs/session",                          // ALWAYS present
"started_at": "...",
"warnings": []
```

Notes:

- `content_b64` is the full patched kubeconfig (round-tripped via ruamel,
  comments/order preserved); only the current-context cluster's `server:` is
  rewritten and `tls-server-name` injected. Other clusters are byte-stable.
- `endpoint`/`certificate_authority_data`/`client_certificate_data`/
  `client_key_data`/`tls_server_name` are the **already-extracted** fields the
  HCL consumer needs directly — this is what removes the HCL reconstruction.
- `path` is non-null only when `daemon.materialize=true`.

## Execution flow (kube mode)

For each `<name> → KubeTarget` on a node, within the already-open SSH session:

1. **Fetch** `kubeconfig_path` over SFTP (reuse `fetcher.py`: same 1 MiB cap and
   error classification). Failure → fails the node if `required=true`.
2. **Parse + select current-context.** The fetched kubeconfig is **untrusted
   external input**: parse with `ruamel.yaml` `YAML(typ="rt")` (round-trip, no
   unsafe constructors). Catch **specific** parse errors
   (`ruamel.yaml.error.YAMLError` and subclasses) and convert them into a
   per-target error — a malformed kubeconfig must fail only its own target, never
   crash the multiplexing daemon or affect other healthy tunnels. No blanket
   `except Exception`. Then find `current-context` → its context → cluster name +
   user name; extract `cluster.server`, `cluster.certificate-authority-data`,
   `user.client-certificate-data`, `user.client-key-data`. No `current-context`
   or unresolvable context → target error. If the file has more than one
   context/cluster, emit a `warnings[]` entry naming the ignored ones.
3. **Resolve server host** on the SSH-server side. Parse
   `server: https://<host>:<port>`; resolve `<host>` to an address as seen by
   the SSH server (so split-horizon DNS is correct). asyncssh resolves the
   forward target server-side when opening a `direct-tcpip` channel, which covers
   the common case; an explicit server-side resolution step may be used where
   needed.
4. **Open local-forward** `127.0.0.1:<os-assigned>` → (via SSH server) →
   `<host>:<port>`. Record `local_port`.
5. **SAN probe.** If `tls_server_name` is set, skip and use it. Otherwise do a
   TLS handshake to the apiserver (through the forward / server-side), read the
   serving certificate, and choose a name: prefer the original `server:` host if
   present in SAN; else first DNS SAN; else first IP SAN. If the chosen name is
   not an exact match of the original `server:` host (a fallback fired), emit a
   `warnings[]` entry naming the chosen SAN. If no usable name and
   `insecure_fallback=false` → target error (fail-fast); if
   `insecure_fallback=true` → mark insecure and emit a `warnings[]` entry
   recording that verification was disabled for the target.
6. **Patch.** Rewrite the current-context cluster's `server:` to
   `https://127.0.0.1:<local_port>` and set `tls-server-name` (or
   `insecure-skip-tls-verify: true` + drop CA on insecure fallback). Leave other
   clusters untouched. `ruamel` dump → `content_b64`.
7. **Materialize** (if `daemon.materialize=true`): write the patched kubeconfig
   to `<session-dir>/tunnel-data/<node>-<name>` (mode 0600); set `path`.
   Otherwise `path=null`.
8. **Assemble** the kube output entry (fields above).

New dependency: `ruamel.yaml`.

## Session directory, materialization, lifecycle

### Layout

The daemon's identity store evolves from a single `<token-dir>/<pid>` file to a
well-known subdirectory:

```
<session-dir>/
  tunnel-data/            # well-known name; daemon owns this entirely
    daemon.pid
    token                 # mode 0600
    <node>-<kube_target>  # only when materialize=true, mode 0600
```

`tunnel-data/` is created mode 0700 (umask); files inside 0600.

### `start --session-dir` semantics

- **Not provided:** daemon generates a session dir (as the current token-dir
  scheme does), creates `tunnel-data/` inside it, works there, and returns the
  path in `session_dir`.
- **Provided:** daemon creates/uses `tunnel-data/` inside the given path, works
  there, and still returns the path in `session_dir`.

`session_dir` is therefore **always** present in the output.

### Untrusted-path handling (`--session-dir`)

`--session-dir` is **untrusted external input** and must be validated before any
filesystem side effect (`global-rules` `code.md:51-53`):

- Normalize/canonicalize the path (`Path.resolve(strict=False)`); require an
  absolute path.
- Create `tunnel-data/` with mode `0700`.
- If `tunnel-data/` already exists, it must be a real directory (not a symlink,
  not a regular file) **owned by the current user**; otherwise refuse to start
  with a clear error. This also covers the `kill -9` orphan case (see Known
  limitation).
- Before cleanup, verify the `tunnel-data/` to be removed is the daemon-owned
  canonical directory (resolve symlinks, check ownership/inode). The
  implementation removes only that resolved daemon-owned directory; it never
  follows an attacker-controlled symlink out of the session dir.

### Cleanup

The daemon removes its working data:

1. **`stop`** — after a confirmed identity match + kill.
2. **daemon `atexit` + SIGTERM handler** — on graceful exit, including
   `auto_stop_idle_seconds` self-shutdown.

What is removed:

- If the session dir was **generated** by the daemon → `rm -rf <session-dir>`
  (whole dir, no leftover).
- If the session dir was **supplied** by the caller → `rm -rf
  <session-dir>/tunnel-data` only (the caller's directory is never removed).

The daemon records whether it generated the dir, so the branch is deterministic.

No startup sweep is performed.

### Known limitation

`kill -9` bypasses `atexit`/signal handlers, leaving an orphaned `tunnel-data/`.
No startup sweep is performed (explicit YAGNI decision). Two mitigations bound
the blast radius:

- `auto_stop_idle_seconds` (graceful self-shutdown triggers `atexit`).
- On the next `start` against a supplied `--session-dir`, an existing
  `tunnel-data/` causes the daemon to **refuse to start with a clear error**
  (see Untrusted-path handling), surfacing the stale state rather than silently
  reusing or clobbering it.

When `materialize=true`, an orphaned `tunnel-data/` may contain a decrypted
kubeconfig with private-key data; operators must clean it up manually
(`rm -rf <session-dir>/tunnel-data`). Documented in the README.

## CLI changes

- **`start`** — gains `--session-dir <path>` (optional). Creates/uses
  `tunnel-data/` inside it (validated per Untrusted-path handling); generates one
  if omitted. Always emits `session_dir`.
- **`stop`** — takes `--session-dir <path>`: reads `daemon.pid` and `token` from
  `<path>/tunnel-data/`, kills, then removes `tunnel-data` (or the whole
  generated dir). The legacy `stop --pid <pid> --token <token>` invocation is
  **removed** (see Decision log) — `--session-dir` is the only stop interface.
- **`status`** — unchanged.

## Security

The README "content never to disk" section is rewritten to document opt-in
materialization:

- Default (`materialize=false`): unchanged guarantee — content travels only via
  the IPC pipe to the parent's stdout; nothing written to disk.
- `materialize=true`: patched content (including private keys embedded in the
  kubeconfig) is written mode 0600 into `<session-dir>/tunnel-data/`, and removed
  on `stop`/`atexit`. Callers opting in accept this trade-off.

**Host-key verification — threat model.** Verification remains not enforced, the
project's existing posture for disposable/trusted hosts. This is re-justified
here because kube mode now reads private keys/certs and probes TLS over the SSH
transport: a MITM on an unverified SSH connection could swap the kubeconfig or
the SAN-probe result. The accepted rationale is that the tool targets
disposable/CI hosts on trusted networks where the SSH endpoint is established
out-of-band by the caller (e.g. infra outputs), and host-key pinning is a
tracked future feature. Operators on untrusted networks must not use kube mode
until pinning lands. SAN probing reads only the remote serving certificate's SAN
extension.

## HCL consumer impact (separate migration phase, in garuda-repo / test-config / v-xxl-cx)

This change is implemented in `garuda-tunnel`. The consumer migration is a
separate phase (different repos) and is described here for context only.

### `terragrunt.hcl` tunnel-up

Replace the kubeconfig `remote_targets` + `fetch_files` with `kube_targets`, pass
a session dir, and (optionally) `materialize = true`:

```hcl
kube_targets = { k3s = { kubeconfig_path = "/etc/rancher/k3s/k3s.yaml" } }
```

### `terragrunt.hcl` tunnel-down

The two `jq` calls disappear:

```bash
[ -d "$SESSION_DIR/tunnel-data" ] || exit 0
uvx --from git+https://github.com/AlexMKX/garuda-tunnel.git@main \
  garuda-tunnel stop --session-dir "$SESSION_DIR" || true
```

(The mock-state short-circuit stays — it is a Terragrunt lifecycle concern, not
a tunnel concern.)

### `locals.tf`

The ~75-line reconstruction collapses to direct field reads:

```hcl
edges_endpoint = { for k in keys(var.edges) :
  k => try(local.tunnel.connections[k].kube_targets.k3s.endpoint, "https://127.0.0.1:0") }

edges_kubeconfig = { for k in keys(var.edges) :
  k => {
    ca              = try(local.tunnel.connections[k].kube_targets.k3s.certificate_authority_data, "")
    cert            = try(local.tunnel.connections[k].kube_targets.k3s.client_certificate_data, "")
    key             = try(local.tunnel.connections[k].kube_targets.k3s.client_key_data, "")
    tls_server_name = try(local.tunnel.connections[k].kube_targets.k3s.tls_server_name, "")
  } }
```

No `yamldecode`, no projection, no type-unification gymnastics. `tls_server_name`
becomes available to the providers (it was absent before — a latent TLS-verify
bug). Providers in `providers.tf` pass `tls_server_name` alongside
host/ca/cert/key.

## Testing strategy

All tests follow the repository's `testing.md` conventions: English-only
docstrings using the standard template (`Validates: / Code: / Assertion: /
Method:`), `unit` / `integration` markers (already defined in `pyproject.toml`),
and the **behavior-over-shape** rule — each case asserts observable behavior, not
mere field existence.

### Unit (marker: `unit`)

- `KubeTarget` schema validation: a tilde/relative/over-length `kubeconfig_path`
  is *rejected* with a specific error; defaults resolve to
  `insecure_fallback=false`, `required=true` (assert the resolved values, not
  just that fields exist).
- kubeconfig parse + current-context selection, using examples 1-3 as fixtures
  (internal-IP server with reordered keys + comments; hostname/split-horizon DNS
  server; multi-context single-cluster file): assert the selected cluster/user
  and that a multi-context file emits the documented `warnings[]` entry.
- safe parsing: a malformed kubeconfig raises a caught `YAMLError` that becomes a
  *per-target error* (assert the daemon/other targets are unaffected), never a
  blanket crash.
- SAN selection: prefer original-server host; fallback first DNS SAN; fallback
  first IP SAN (assert a `warnings[]` entry on non-exact match); empty SAN →
  `insecure_fallback` branch — `false` produces a target error, `true` produces
  `insecure-skip-tls-verify` + warning.
- patch: `server:` rewrite + `tls-server-name` on the correct cluster; other
  clusters byte-stable; comment/key-order preservation (round-trip ruamel).
- session dir: `tunnel-data` created mode 0700; an existing/symlinked/foreign
  `tunnel-data` causes `start` to refuse; cleanup removes generated dir wholesale
  vs only `tunnel-data` for a supplied dir.
- output: `kube_targets` section present with the documented flat fields;
  `session_dir` always present.

### Integration (marker: `integration`; Docker sshd + a fake apiserver presenting a TLS cert with SAN)

- end-to-end: `kube_target` → forward → SAN probe → patched kubeconfig usable
  through the forward (kubectl-equivalent TLS handshake succeeds).
- `materialize=true` → file on disk, mode 0600, removed on `stop`.
- `insecure_fallback=true` with a SAN-less cert → insecure kubeconfig emitted +
  warning.

### Verification gates (done criteria, per `AGENTS.md` + `specific/python.md`)

Before merge, all must pass:
`ruff format --check`, `ruff check`, `mypy --strict`, `pylint` (fail-under 9.0),
`vulture` (and a post-implementation dead-code sweep — ask the user before
removing anything found), `pytest` (unit), `pytest -m integration`, coverage
≥ 80%.

## Documentation deliverables

Per `global-rules` layered-documentation requirements:

- Docstrings (English) for the new `KubeTarget` model, the `NodeOutput`
  `kube_targets` entry, and the kube patcher functions — documenting *why* (e.g.
  why `current-context` only, why SAN-probe).
- `--session-dir` help text on `start`/`stop`.
- Package README: a kube-mode section, the opt-in materialization note, the
  rewritten security/host-key section, and a migration note for the removed
  `stop --pid --token`.

## Versioning

Additive at the field level (no field removed or renamed), but **not strictly
backward compatible**: `stop --pid --token` is removed, and a new always-present
`session_dir` output field plus a per-node `kube_targets` output section are
introduced. README documents the migration.

## Decision log

- **Backward compatibility removed (not maintained).** Per `global-rules`
  (`code.md:56-59`) BC is not carried without explicit instruction; the user
  explicitly chose to drop the legacy `stop --pid --token` interface in favor of
  `stop --session-dir` (brainstorming, 2026-05-30).
- **`insecure_fallback` fallback plan** is fixed upfront (scope, trigger,
  effect, observability, retirement) — see Key invariants.
- **`ruamel.yaml>=0.18,<0.19`**, round-trip mode, verified maintained as of
  2026.
- **Example domain only** (`example.net`) in committed docs, per `AGENTS.md`.
