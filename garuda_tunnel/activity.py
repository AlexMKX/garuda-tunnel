"""Forward-connection activity tracker for idle-based auto-stop.

The tracker implements the :class:`asyncssh.ForwardTracker` Protocol
(duck-typed). One instance is owned by :class:`~garuda_tunnel.manager.TunnelManager`
and passed down to every ``forward_local_port`` call, so all node forwards
share a single aggregate counter.
"""

from __future__ import annotations

import time


class ActivityTracker:
    """Aggregate counter + last-activity timestamp shared across all forwards.

    Implements asyncssh.ForwardTracker (PEP 544 Protocol — duck-typed).
    All methods run on the asyncio loop thread; no locking needed.

    Public properties ``is_idle`` and ``seconds_since_activity`` answer
    the daemon-wide question "should auto-stop fire now?".
    """

    def __init__(self) -> None:
        self._active_count = 0
        self._last_activity_at = time.monotonic()

    # asyncssh.ForwardTracker hooks ----------------------------------------

    def connection_made(self, orig_host: str, orig_port: int) -> None:
        """asyncssh hook: a new client TCP connection was accepted."""
        del orig_host, orig_port  # Per-connection detail unused for aggregates.
        self._active_count += 1
        self._last_activity_at = time.monotonic()

    def connection_lost(self, orig_host: str, orig_port: int, exc: Exception | None) -> None:
        """asyncssh hook: a previously-accepted connection has closed."""
        del orig_host, orig_port, exc
        self._active_count = max(0, self._active_count - 1)
        self._last_activity_at = time.monotonic()

    # Aggregate query API --------------------------------------------------

    @property
    def is_idle(self) -> bool:
        """True iff there are zero active forward connections right now."""
        return self._active_count == 0

    @property
    def seconds_since_activity(self) -> float:
        """Monotonic-clock seconds since the most recent open/close event."""
        return time.monotonic() - self._last_activity_at
