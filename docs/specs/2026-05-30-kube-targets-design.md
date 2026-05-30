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
- No breaking change: `kube_targets`, `materialize`, and `--session-dir` are
  all additive. `remote_targets`, `fetch_files`, and `stop --pid --token`
  continue to work.

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
  connections.
- **cluster↔user binding via `current-context`.** The `current-context` names
  both the cluster and the user; CA comes from the cluster, client cert/key from
  the user.
- **Server host resolution happens on the SSH-server side**, so split-horizon
  DNS (a hostname that resolves differently inside the server's network)
  resolves correctly.
- **TLS server name selection:** prefer the host from the original `server:`
  URL if it appears in the certificate SAN; else the first DNS-type SAN; else
  the first IP-type SAN. An explicit `KubeTarget.tls_server_name` overrides the
  probe entirely.
- **`insecure_fallback` is `false` by default** (secure by default). If the SAN
  probe yields no usable name and no explicit `tls_server_name` is given, the
  target fails (subject to `required`). When set to `true`, the patcher emits
  `insecure-skip-tls-verify: true`, drops `certificate-authority-data`, and adds
  a warning.
- **`materialize` is `false` by default**, preserving the existing
  "content never to disk" guarantee for current consumers.
- **YAML library: `ruamel.yaml`** (round-trip), preserving comments and key
  order in the patched kubeconfig.
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

> `daemon.runtime_dir` is **not** added. Session-dir location is controlled via
> the `start --session-dir` CLI flag (see CLI changes) so that the same path can
> be handed to `stop --session-dir`. The default location (when no flag is
> given) remains the current token directory.

### Output

`NodeOutput` gains a `kube` section alongside `ports` and `fetch_files`. Because
of the one-cluster invariant, each kube entry is flat (no nested clusters map):

```jsonc
"connections": {
  "hub": {
    "ports": { /* existing, from remote_targets */ },
    "fetch_files": { /* existing */ },
    "kube": {
      "<kube_target_name>": {
        "cluster_name": "production",                  // from current-context
        "context_name": "production",                  // current-context name
        "local_port": 40123,
        "endpoint": "https://127.0.0.1:40123",
        "tls_server_name": "am.prod.kube.gfn.team",    // chosen SAN / hint / null if insecure
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
2. **Parse + select current-context.** `ruamel.yaml` load; find
   `current-context` → its context → cluster name + user name; extract
   `cluster.server`, `cluster.certificate-authority-data`,
   `user.client-certificate-data`, `user.client-key-data`. No `current-context`
   or unresolvable context → target error.
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
   present in SAN; else first DNS SAN; else first IP SAN. If none and
   `insecure_fallback=false` → target error; if `insecure_fallback=true` → mark
   insecure and warn.
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

`kill -9` bypasses `atexit`/signal handlers, leaving an orphaned
`tunnel-data/`. Mitigated by `auto_stop_idle_seconds` (graceful self-shutdown
triggers `atexit`). Documented in the README.

## CLI changes

- **`start`** — gains `--session-dir <path>` (optional). Creates/uses
  `tunnel-data/` inside it; generates one if omitted. Always emits `session_dir`.
- **`stop`** — gains `--session-dir <path>` (new, recommended): reads `daemon.pid`
  and `token` from `<path>/tunnel-data/`, kills, then removes `tunnel-data` (or
  the whole generated dir). Mutually exclusive with `--pid/--token`. The legacy
  `stop --pid <pid> --token <token>` continues to work against the default path.
- **`status`** — unchanged.

## Security

The README "content never to disk" section is rewritten to document opt-in
materialization:

- Default (`materialize=false`): unchanged guarantee — content travels only via
  the IPC pipe to the parent's stdout; nothing written to disk.
- `materialize=true`: patched content (including private keys embedded in the
  kubeconfig) is written mode 0600 into `<session-dir>/tunnel-data/`, and removed
  on `stop`/`atexit`. Callers opting in accept this trade-off.

Host-key verification remains not enforced (existing posture). SAN probing
reads only the remote serving certificate's SAN extension.

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
  k => try(local.tunnel.connections[k].kube.k3s.endpoint, "https://127.0.0.1:0") }

edges_kubeconfig = { for k in keys(var.edges) :
  k => {
    ca              = try(local.tunnel.connections[k].kube.k3s.certificate_authority_data, "")
    cert            = try(local.tunnel.connections[k].kube.k3s.client_certificate_data, "")
    key             = try(local.tunnel.connections[k].kube.k3s.client_key_data, "")
    tls_server_name = try(local.tunnel.connections[k].kube.k3s.tls_server_name, "")
  } }
```

No `yamldecode`, no projection, no type-unification gymnastics. `tls_server_name`
becomes available to the providers (it was absent before — a latent TLS-verify
bug). Providers in `providers.tf` pass `tls_server_name` alongside
host/ca/cert/key.

## Testing strategy

### Unit

- `KubeTarget` schema validation (path rules; `tls_server_name`/`insecure_fallback`
  defaults).
- kubeconfig parse + current-context selection, using examples 1-3 as fixtures
  (internal-IP server with reordered keys + comments; hostname/split-horizon DNS
  server; multi-context single-cluster file).
- SAN selection: prefer original-server host; fallback first DNS SAN; fallback
  first IP SAN; empty SAN → `insecure_fallback` branch (both `true` and `false`).
- patch: `server:` rewrite + `tls-server-name` on the correct cluster; other
  clusters byte-stable; comment/key-order preservation (round-trip ruamel).
- session dir: `tunnel-data` creation; cleanup of generated vs supplied dir.
- output shape (kube section, flat fields, `session_dir` always present).

### Integration (Docker sshd + a fake apiserver presenting a TLS cert with SAN)

- end-to-end: `kube_target` → forward → SAN probe → patched kubeconfig usable
  through the forward (kubectl-equivalent TLS handshake succeeds).
- `materialize=true` → file on disk, mode 0600, removed on `stop`.
- `insecure_fallback=true` with a SAN-less cert → insecure kubeconfig emitted +
  warning.

## Versioning

Additive, non-breaking. README gains a kube-mode section, an opt-in
materialization note, and a rewritten security section.
