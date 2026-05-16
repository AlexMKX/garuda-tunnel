# garuda-tunnel

SSH tunnel manager for ephemeral execution environments (CI runners,
disposable containers, Terragrunt hooks). Opens N SSH local-forward tunnels
in one call, returns the resulting `127.0.0.1:port` mapping plus a daemon
PID and identity token as JSON, then detaches as a background daemon.

## Quickstart

Latest released tag: `v2026.10516.11702`.

```bash
pipx run --spec git+https://github.com/AlexMKX/garuda-tunnel.git@v2026.10516.11702 \
    garuda-tunnel --help
```

If you have SSH access to GitHub configured for this account, the SSH URL
form also works:

```bash
pipx run --spec git+ssh://git@github.com/AlexMKX/garuda-tunnel.git@v2026.10516.11702 \
    garuda-tunnel --help
```

## Usage

```text
Usage: garuda-tunnel [OPTIONS] COMMAND [ARGS]...

  garuda-tunnel: SSH tunnel manager for ephemeral environments.

Options:
  --version  Show the version and exit.
  --help     Show this message and exit.

Commands:
  start   Read JSON from stdin, open tunnels, daemonize, print mapping JSON.
  status  Report whether the daemon with the given PID (and optional ...) is alive.
  stop    Send SIGTERM (then SIGKILL) to a garuda-tunnel daemon ...
```

`start` reads a JSON `InputSchema` from stdin and writes the JSON
`OutputSchema` to stdout, including the daemon `pid` and identity `token`
the caller must save for `stop` / `status`. See
`docs/specs/2026-05-16-garuda-tunnel-design.md` for the full I/O contract.

## Running tests

Unit tests:

```bash
pip install -e ".[dev]"
pytest tests/unit
```

Integration tests (require Linux + Docker Compose v2):

```bash
pytest tests/integration -m integration
```

The integration suite spins up a 3-node `lscr.io/linuxserver/openssh-server`
cluster via `tests/integration/docker-compose.yml` and exercises the full
`start` / `stop` / `status` flow against real SSH daemons.

## Project documents

- Spec: `docs/specs/2026-05-16-garuda-tunnel-design.md`
- Implementation plan: `docs/superpowers/plans/2026-05-16-garuda-tunnel.md`

## License

MIT.
