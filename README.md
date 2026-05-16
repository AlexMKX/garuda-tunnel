# garuda-tunnel

SSH tunnel manager for ephemeral execution environments (CI runners,
disposable containers, Terragrunt hooks). Opens N SSH local-forward tunnels
in one call, returns the resulting `127.0.0.1:port` mapping plus a daemon
PID and identity token as JSON, then detaches as a background daemon.

## Quickstart

```bash
pipx run --spec git+ssh://git@github.com/AlexMKX/garuda-tunnel.git@<TAG> garuda-tunnel --help
```

Full design: `docs/specs/2026-05-16-garuda-tunnel-design.md`.
Implementation plan: `docs/superpowers/plans/2026-05-16-garuda-tunnel.md`.

## License

MIT.
