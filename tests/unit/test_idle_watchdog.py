"""Unit tests for the idle watchdog coroutine."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from garuda_tunnel._worker import _idle_watchdog
from garuda_tunnel.activity import ActivityTracker

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_no_activity_triggers_stop() -> None:
    """Empty tracker triggers stop_event within timeout window."""
    tracker = ActivityTracker()
    stop_event = asyncio.Event()
    await asyncio.wait_for(
        _idle_watchdog(tracker, timeout_seconds=1, stop_event=stop_event),
        timeout=3.0,
    )
    assert stop_event.is_set()


@pytest.mark.asyncio
async def test_active_connection_blocks_stop() -> None:
    """Open connection prevents stop_event from being set."""
    tracker = ActivityTracker()
    ct = tracker.make_tracker()
    ct.connection_made(MagicMock(), "h", 1)
    stop_event = asyncio.Event()
    task = asyncio.create_task(_idle_watchdog(tracker, timeout_seconds=1, stop_event=stop_event))
    await asyncio.sleep(2.5)
    assert not stop_event.is_set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_lost_after_idle_triggers_stop() -> None:
    """made + lost + wait fires the watchdog."""
    tracker = ActivityTracker()
    ct = tracker.make_tracker()
    ct.connection_made(MagicMock(), "h", 1)
    ct.connection_lost(None)
    stop_event = asyncio.Event()
    await asyncio.wait_for(
        _idle_watchdog(tracker, timeout_seconds=1, stop_event=stop_event),
        timeout=3.0,
    )
    assert stop_event.is_set()


@pytest.mark.asyncio
async def test_cancellation_returns_cleanly() -> None:
    """task.cancel() ends the coroutine without raising."""
    tracker = ActivityTracker()
    stop_event = asyncio.Event()
    task = asyncio.create_task(_idle_watchdog(tracker, timeout_seconds=60, stop_event=stop_event))
    await asyncio.sleep(0.1)  # let it enter the sleep
    task.cancel()
    # Should not raise; CancelledError handled inside.
    result = await task
    assert result is None
    assert not stop_event.is_set()


@pytest.mark.asyncio
async def test_returns_early_when_stop_event_externally_set() -> None:
    """An externally-set stop_event ends the loop on next check."""
    tracker = ActivityTracker()
    ct = tracker.make_tracker()
    ct.connection_made(MagicMock(), "h", 1)
    stop_event = asyncio.Event()
    task = asyncio.create_task(_idle_watchdog(tracker, timeout_seconds=1, stop_event=stop_event))
    await asyncio.sleep(0.1)
    stop_event.set()
    # Watchdog wakes from its sleep, re-checks `while not stop_event.is_set()`,
    # exits the loop.
    await asyncio.wait_for(task, timeout=2.0)
