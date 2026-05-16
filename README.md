# garuda-tunnel

SSH tunnel manager for ephemeral execution environments (CI runners, disposable
containers, Terragrunt hooks). Opens N SSH local-forward tunnels in one call,
returns the resulting `127.0.0.1:port` mapping as JSON, and daemonizes.

**Status**: in design (spec at `docs/specs/2026-05-16-garuda-tunnel-design.md`).
No code yet.

## License

MIT.
