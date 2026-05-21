# garuda-tunnel

> Open N SSH local-forward tunnels and fetch small remote config files in a
> single bootstrap. Built for disposable CI / operator environments that talk
> to k3s or similar internal services without public ingress.

**Audience:** infrastructure engineers running short-lived jobs (CI, local
containers, Terragrunt hooks) that need SSH-tunneled access plus a kubeconfig
(or similar config file) from one or more remote hosts. The tool is generic
— it does not depend on Kubernetes — but the motivating use case is k3s edge
nodes whose apiserver binds to `127.0.0.1` only.

## Why this exists

- Internal apiservers (k3s, gitea, registries) are not publicly exposed; SSH
  is the only audited path in.
- Tools like `helm` / `kubectl` need an endpoint **and** a kubeconfig at
  plan/apply time — pulling both in one bootstrap avoids a second
  authentication and a second sshd session.
- Ephemeral environments cannot rely on persistent SSH setup, agent
  forwarding, or pre-installed kubeconfigs.

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

## End-to-end example

```bash
#!/usr/bin/env bash
set -euo pipefail

PRIVATE_KEY=$(cat ~/.ssh/id_ed25519)

JSON=$(cat <<EOF
{
  "nodes": {
    "edge1": {
      "host": "198.51.100.10",
      "user": "root",
      "ssh_pkey": $(jq -Rs . <<<"$PRIVATE_KEY"),
      "remote_targets": {"kubeapi": "127.0.0.1:6443"},
      "required": true,
      "fetch_files": {
        "kubeconfig": {"path": "/etc/rancher/k3s/k3s.yaml"}
      }
    }
  },
  "daemon": {
    "auto_stop_idle_seconds": 600
  }
}
EOF
)

RESULT=$(echo "$JSON" | garuda-tunnel start)

PID=$(jq -r '.pid' <<<"$RESULT")
TOKEN=$(jq -r '.token' <<<"$RESULT")
PORT=$(jq -r '.connections.edge1.ports.kubeapi' <<<"$RESULT")
KUBECONFIG_B64=$(jq -r '.connections.edge1.fetch_files.kubeconfig.content_b64' <<<"$RESULT")

KUBECONFIG_FILE=$(mktemp)
base64 -d <<<"$KUBECONFIG_B64" >"$KUBECONFIG_FILE"
sed -i "s|server: https://127.0.0.1:6443|server: https://127.0.0.1:${PORT}|" \
    "$KUBECONFIG_FILE"

kubectl --kubeconfig="$KUBECONFIG_FILE" get nodes

garuda-tunnel stop --pid "$PID" --token "$TOKEN"
rm -f "$KUBECONFIG_FILE"
```

The `daemon.auto_stop_idle_seconds: 600` setting makes the daemon shut
itself down after 10 minutes with no client connections. Useful for
ephemeral CI runs that may abort before reaching `garuda-tunnel stop`.
Omit the field (or set to `null`) to keep the daemon alive until you call
`stop` explicitly.

## Input reference (`InputSchema`)

**Top level**

| Field | Type | Default | Description |
|---|---|---|---|
| `nodes` | `dict[str, NodeInput]` | required | One entry per remote host |
| `daemon.log_file` | `str \| null` | `null` | If set, daemon's stdout/stderr go here. Never contains fetched content. |
| `daemon.shutdown_grace_seconds` | `int` | `10` | SIGTERM grace period before SIGKILL |
| `daemon.auto_stop_idle_seconds` | `int \| null` | `null` | Seconds of idle (no active forward connections) before the daemon SIGTERMs itself. `null` disables. |

**`NodeInput`** (per entry in `nodes`)

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | `str` | required | Remote SSH hostname or IP |
| `port` | `int` | `22` | Remote SSH port |
| `user` | `str` | required | Remote SSH user |
| `ssh_pkey` | `str \| null` | `null` | PEM-encoded private key (in-memory, never written) |
| `ssh_password` | `str \| null` | `null` | Password fallback. Either `ssh_pkey` or `ssh_password` must be set. |
| `ssh_pkey_passphrase` | `str \| null` | `null` | Optional passphrase for `ssh_pkey` |
| `remote_targets` | `dict[str, str]` | required | 1..16 entries; each value is `"host:port"`. Host is resolved on the SSH server side, enabling bastion-style cross-host forwards. |
| `ssh_options.compression` | `bool` | `false` | Enable SSH compression |
| `ssh_options.connect_timeout` | `int` | `60` | Seconds |
| `required` | `bool` | `true` | If false, this node may fail without aborting `start` |
| `fetch_files` | `dict[str, FileSpec] \| null` | `null` | Files to read at start (max 16) |

**`FileSpec`** (per entry in `fetch_files`)

| Field | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | required | Absolute remote path (no `~`, no `$VAR` expansion) |
| `required` | `bool` | `true` | If false, fetch failure does not fail the node |

Constraints:
- `fetch_files` logical name: `^[a-zA-Z_][a-zA-Z0-9_-]*$`, 1..64 chars
- `FileSpec.path`: starts with `/`, 1..4096 chars
- Per-file size cap: 1 MiB (exceeded → `EFBIG`)
- Host key verification: **not enforced** in this release. Use on trusted
  networks or with disposable hosts.

## Output reference

**Success (`OutputSchema`)**

```jsonc
{
  "connections": {
    "edge1": {
      "ports": {
        "kubeapi": 40123
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
  "started_at": "2026-05-19T10:00:00Z",
  "warnings": []
}
```

**Failure (`ErrorOutput`)**

```json
{
  "error": "RequiredTunnelFailure",
  "message": "required tunnel(s) failed to start",
  "details": {
    "failed": [
      {"node": "edge1", "error": "required fetch_files failed: ['kubeconfig']"}
    ]
  }
}
```

Always inspect the top-level `error` key first to distinguish success from
failure.

## Error reference (`fetch_files[name].error`)

| Value | Meaning | First remediation |
|---|---|---|
| `SSH_FX_NO_SUCH_FILE` | Path doesn't exist | `ssh user@host ls -la <path>` |
| `SSH_FX_PERMISSION_DENIED` | File ACL blocks the SSH user | Check ownership/mode |
| `SSH_FX_FAILURE` | Generic server-side SFTP failure | Inspect remote sshd logs |
| `SSH_FX_NO_CONNECTION` | SFTP subsystem rejected the channel | Verify `Subsystem sftp` in `sshd_config` |
| `SSH_FX_CONNECTION_LOST` | Channel died mid-read | Network instability; retry |
| `SSH_FX_OP_UNSUPPORTED` | Server doesn't implement the operation | Non-OpenSSH SFTP server; not supported |
| `EFBIG` | File exceeds the 1 MiB hard cap | This tool is for configs, not blobs |
| `ChannelOpenError` / `ConnectionResetError` / `TimeoutError` | Transport-level failure | Network or sshd config issue |
| `RuntimeError` | Internal state issue | Check stderr and `daemon.log_file` |

## Security notes

- File content travels exactly once: from the daemon to the parent process
  via an IPC pipe, then to the parent's stdout. The tool itself never writes
  content to disk.
- `daemon.log_file` (if set) receives only asyncssh/asyncio debug noise. No
  `print`/`log` call path in this codebase carries decoded file bytes.
- `content_b64` is base64; callers must decode and protect it.
- `token` returned by `start` is the authorization handle for `stop`/`status`.
  Store it like a credential.
- Private keys (`ssh_pkey`) stay in process memory; they are never written
  to `~/.ssh` or to a tempfile. Parsing happens via
  `asyncssh.import_private_key`.
- This release does **not** verify remote host keys. Use only on trusted
  networks or with disposable hosts. A pinning option is a future feature.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `error: SchemaValidationError`, `details` mentions `require` | Old top-level `require` field. Use per-node `required: bool` instead. |
| `error: SchemaValidationError`, `details` mentions `connections[...]` | Old output shape. `connections[node]` is now `{ports, fetch_files}`, not a list. |
| `error: SchemaValidationError`, `details` mentions `remote_ports` | The old `remote_ports: list[int]` field is gone. Use `remote_targets: {"handle": "host:port"}`. |
| `fetch_files[name].error == "EFBIG"` | File exceeds 1 MiB. Wrong file, or this tool isn't the right transport. |
| `fetch_files[name].error == "SSH_FX_PERMISSION_DENIED"` | The SSH user lacks read on the file. Check ACLs. |
| `start` hangs | Node firewalled / DNS-stuck. Increase `ssh_options.connect_timeout` or remove the node. |
| `status` says alive but `stop` says "token mismatch" | The PID was reused. Token guards against this — investigate which process holds the PID. |

## Migration from `v2026.10516.11702`

Two breaking changes:

**Output shape**

```diff
- jq '.connections.edge1[0].local_port'
+ jq '.connections.edge1.ports.kubeapi'
```

**Input require → per-node required**

```diff
  nodes:
    edge1: {host: ..., remote_targets: {kubeapi: "127.0.0.1:6443"}}
-   edge2: {host: ..., remote_targets: {kubeapi: "127.0.0.1:6443"}}
+   edge2: {host: ..., remote_targets: {kubeapi: "127.0.0.1:6443"}, required: false}
- require: ["edge1"]
```

Pydantic's `extra=forbid` on `InputSchema` rejects the old `require` field
with a clear error.

**Remote targets**

```diff
- remote_ports: [6443]
+ remote_targets: {kubeapi: "127.0.0.1:6443"}
```

```diff
- jq '.connections.edge1.ports[0].local_port'
+ jq '.connections.edge1.ports.kubeapi'
```

Previous `remote_ports: list[int]` implied `127.0.0.1` on the SSH server.
New `remote_targets` makes the target host explicit, enabling
bastion-style forwards to other hosts in the SSH server's network.
`local_ports` is removed — local listeners are always OS-assigned.

**Removed `ssh_options` fields:** `host_key_policy`, `known_hosts_path`, `threaded` (unused since the asyncssh migration; `extra=forbid` rejects them).

## Running tests

Unit:

```bash
pip install -e ".[dev]"
pytest tests/unit
```

Integration (Linux + Docker Compose v2):

```bash
pytest tests/integration -m integration
```

## Project documents

- Current design: `docs/specs/2026-05-20-feature-fetch-files-design.md`
- Original design (historical): `docs/specs/2026-05-16-garuda-tunnel-design.md`

## License

MIT.
