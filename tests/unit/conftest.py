"""Unit-test helpers (shared ``make_node`` payload factory)."""

from __future__ import annotations

from typing import Any


def make_node(**overrides: Any) -> dict[str, Any]:
    """Minimal valid NodeInput payload for tests."""
    base: dict[str, Any] = {
        "host": "node1.example.net",
        "user": "ubuntu",
        "ssh_password": "p",
        "remote_targets": {"p": "127.0.0.1:6443"},
    }
    base.update(overrides)
    return base
