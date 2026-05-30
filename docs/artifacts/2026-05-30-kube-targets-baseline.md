# Baseline: kube-targets feature branch (2026-05-30)

Branch: `feature/kube-targets`
Worktree: `garuda-tunnel/.worktrees/kube-targets`
Date: 2026-05-30

Note: `.venv` was absent from the worktree; created fresh via
`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` before recording.

## Unit test count

```
150 passed in 17.95s
```

**150 passed.**

## Lint / type gates

| Gate | Command | Result |
|------|---------|--------|
| ruff format | `.venv/bin/ruff format --check garuda_tunnel` | **PASS** |
| ruff check  | `.venv/bin/ruff check garuda_tunnel`          | **PASS** |
| mypy strict | `.venv/bin/mypy --strict garuda_tunnel`        | **PASS** |

### ruff format --check output
```
12 files already formatted
```

### ruff check output
```
All checks passed!
```

### mypy --strict output
```
Success: no issues found in 13 source files
```
