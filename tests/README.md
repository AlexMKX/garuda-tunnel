# Tests

This directory contains two suites:

- `tests/unit/` — pure-Python unit tests. Marked with
  `pytestmark = pytest.mark.unit`. Run with `pytest tests/unit -q`.
- `tests/integration/` — Linux + Docker integration tests. Marked with
  `pytestmark = pytest.mark.integration`. Run with
  `PATH="$PWD/.venv/bin:$PATH" pytest tests/integration -m integration -q`.

## Conventions

- Every test file starts with a module docstring describing what behaviour
  the file covers.
- Every test function has a one-line docstring stating the assertion.
- Test names are imperative: `test_<subject>_does_<behaviour>`.
- Fakes live alongside the tests that need them (no shared mocks module).
- No real network IO in unit tests. Integration tests use dockerized
  `openssh-server` containers from `linuxserver/openssh-server`.

## Fixtures

Defined in `tests/integration/conftest.py`:

- `tunstrap_it_dir` — session-scoped; pre-creates
  `/tmp/tunstrap-it/` with mode `0o1777` so that a docker bind-mount
  cannot lock it to root.
- `ssh_keypair` — generates an ed25519 keypair into
  `tests/integration/_keys/`.
- `ssh_test_cluster` — runs `docker compose up -d --wait` and returns
  the per-service exposed ports.
- `prepared_files` — populates `/tmp/tunstrap-it/{kubeconfig,
  big.bin,no-perm.txt}` for `fetch_files` scenarios.
- `started_daemons` — collects `session_dir` strings from successful start
  invocations so the suite teardown can stop them by `--session-dir`.

## Local prerequisites for integration

- Linux host (macOS works but is slower; CI uses ubuntu-latest).
- Docker Compose v2.
- Python 3.10+ with the project venv installed: `pip install -e ".[dev]"`.
