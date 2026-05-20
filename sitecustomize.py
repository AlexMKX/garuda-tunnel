"""Process-wide hook used by integration tests to collect subprocess coverage.

When the test harness exports ``COVERAGE_PROCESS_START`` pointing at the
project's coverage config, every Python subprocess (here: the
``garuda-tunnel`` CLI) starts coverage measurement immediately on
interpreter init via this hook. Subsequent ``coverage combine`` merges
the per-process ``.coverage.*`` files into a single dataset.

The hook is a no-op when the environment variable is not set, so it does
not affect production execution or unit-test runs.
"""

from __future__ import annotations

import os

if os.environ.get("COVERAGE_PROCESS_START"):  # pragma: no cover - bootstrap path
    try:
        import coverage  # type: ignore[import-not-found]

        coverage.process_startup()
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Never let coverage instrumentation crash the daemon: if the
        # measurement bootstrap fails for any reason, the process must
        # still run normally.
        pass
