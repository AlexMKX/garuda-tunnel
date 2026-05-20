"""Cross-suite teardown: stop daemons left running by integration tests."""

from __future__ import annotations

import subprocess
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def kill_orphan_test_daemons(request: pytest.FixtureRequest) -> Iterator[None]:
    """Stop every garuda-tunnel daemon recorded in ``started_daemons``."""
    # ``started_daemons`` is provided by the integration conftest and is shared
    # across the session; unit tests do not request it and skip cleanup.
    try:
        started: list[tuple[int, str]] = request.getfixturevalue("started_daemons")
    except pytest.FixtureLookupError:
        yield
        return
    yield
    for pid, token in started:
        subprocess.run(
            ["garuda-tunnel", "stop", "--pid", str(pid), "--token", token],
            capture_output=True,
        )
    started.clear()
