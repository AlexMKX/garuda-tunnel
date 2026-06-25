# SSH agent fallback (issue #9)

**Issue:** [AlexMKX/tunstrap#9](https://github.com/AlexMKX/tunstrap/issues/9) —
"make use ssh-agent keys".

> When no key path is provided and no ssh-password usage is specified, the
> tunnel must use already existing ssh key credentials, which are provided by
> agent.

## Goal

When the caller supplies neither an explicit private key nor a password,
`tunstrap` must authenticate over the running `ssh-agent` (via
`$SSH_AUTH_SOCK`) instead of failing schema validation.

## Current behavior (baseline)

- CLI flags `--ssh-key` and `--ssh-password-stdin` are both optional, but the
  `InputSchema._validate_auth` model-validator at `tunstrap/schemas.py:271`
  raises `node {name!r}: must provide ssh_pkey or ssh_password` if both are
  absent.
- `tunstrap/ssh.py::open_connection` builds `asyncssh.connect(**kwargs)` with
  `client_keys=_load_client_keys(node)` (which returns `None` when no
  `ssh_pkey`) and conditionally `password=node.ssh_password`. asyncssh's
  default behavior, when `client_keys is None` and `agent_path` is not
  overridden, already consults `$SSH_AUTH_SOCK`.

## Design

### Semantics

| `ssh_pkey` | `ssh_password` | `$SSH_AUTH_SOCK` | Result                       |
|------------|----------------|------------------|------------------------------|
| set        | any            | any              | use key (today)              |
| unset      | set            | any              | use password (today)         |
| unset      | unset          | set, non-empty   | **use agent (new)**          |
| unset      | unset          | unset / empty    | **schema validation error**  |

Precedence is unchanged: an explicit `ssh_pkey` always wins; an explicit
`ssh_password` is the password path; the agent is only the fallback when both
are absent. We do not silently fall back to interactive prompts.

### Schema change

`tunstrap/schemas.py::InputSchema._validate_auth` is replaced with logic that:

- accepts a node where both `ssh_pkey` and `ssh_password` are unset **iff**
  `os.environ.get("SSH_AUTH_SOCK")` is set and non-empty;
- otherwise raises a `ValueError` whose message names all three options
  (e.g. `"node 'a': provide ssh_pkey, ssh_password, or run ssh-agent
  (SSH_AUTH_SOCK)"`) so the error is actionable in flag-mode and JSON-mode
  alike.

The check is intentionally performed at schema-validation time, not at
connect-time, so:

- `tunstrap start` / `tunstrap run` fail fast with exit code 1 (schema
  validation error, per `exit_code_for`) before any daemon is spawned, and
- both CLI flag mode and JSON-stdin mode get identical behavior with one rule.

### Transport change

`tunstrap/ssh.py::open_connection`: when both `ssh_pkey` and `ssh_password`
are `None`, do not add `client_keys` to the kwargs at all (rather than
passing `client_keys=None`), so that asyncssh's normal agent discovery path
runs. No explicit `agent_path` override is set — asyncssh reads
`$SSH_AUTH_SOCK` itself; that keeps behavior identical to the OpenSSH client
and matches what schema validation already gated on.

### Redaction / exceptions

`tunstrap/exceptions.py::_SECRET_KEYS` already covers `ssh_pkey`,
`ssh_password`, `ssh_pkey_passphrase`. No secret material is added by this
change; no new keys to redact.

### CLI

No new flags. The behavior change is purely "absence of both flags now means
agent" instead of "absence of both flags is a validation error". Help text for
`--ssh-key` updated to document the fallback.

## Tests

### Unit

- `tests/unit/test_schemas.py`: replace `test_node_requires_pkey_or_password`
  with two cases:
  - `SSH_AUTH_SOCK` absent → still rejected, error mentions `ssh-agent`.
  - `SSH_AUTH_SOCK` set to a non-empty placeholder → schema accepts a node
    with neither `ssh_pkey` nor `ssh_password`.
  Use `monkeypatch.setenv` / `monkeypatch.delenv` for isolation.
- `tests/unit/test_ssh_transport.py` (or new
  `tests/unit/test_manager_agent.py`): assert that when both `ssh_pkey` and
  `ssh_password` are `None`, `open_connection`'s call to
  `asyncssh.connect(...)` does NOT include the `client_keys` kwarg AND does
  NOT include `password`. Existing tests covering pkey / password modes
  continue to pass.
- `tests/unit/test_cli_input.py`: add a case where `ssh_key=None` and
  `ssh_password=None` build a valid schema (with `SSH_AUTH_SOCK` set via
  `monkeypatch`).

### Integration (best-effort, gated)

- New case in `tests/integration/test_cli_modes.py`: start a real
  `ssh-agent` in a subprocess, `ssh-add` the existing
  `ssh_test_cluster["private_pem"]` fixture, run
  `tunstrap start root@host:port --target ...` with **no** `--ssh-key` and
  **no** `--ssh-password-stdin`, assert success and that a tunnel comes up.
  Skip cleanly (`pytest.skip`) if `ssh-agent` / `ssh-add` are not on
  `$PATH` so CI without OpenSSH client tools still passes.

## Documentation

- `README.md` quickstart / CLI reference: add a one-line note under the
  authentication section that "when neither `--ssh-key` nor
  `--ssh-password-stdin` is given, tunstrap uses keys from your running
  `ssh-agent` (via `$SSH_AUTH_SOCK`)".

## Out of scope

- No support for `~/.ssh/config` (`IdentityFile`, agent forwarding,
  `IdentitiesOnly`).
- No interactive password prompt.
- No agent-forwarding (`ForwardAgent`) into the remote shell — only client
  authentication.
- No new auth precedence options exposed to callers.

## Risk / compatibility

- Behavior change is strictly relaxing a validation error into success
  in a previously-rejected configuration; no existing accepted input
  changes meaning. Safe for a MINOR version bump.
- Callers that were relying on the validation error to detect "user
  forgot to pass auth" will instead get an agent-auth failure at
  connect-time, with a clear asyncssh error.

## Branch / PR

- Branch: `feature/ssh-agent-fallback` off `main` (a4f086d).
- PR title: `feat(auth): fall back to ssh-agent when neither key nor password is supplied (#9)`.
- Closes #9.
