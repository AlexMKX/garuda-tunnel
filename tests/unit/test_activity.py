"""Unit tests for the activity tracker used by idle-based auto-stop."""

from __future__ import annotations

import time

import pytest

from garuda_tunnel.activity import ActivityTracker

pytestmark = pytest.mark.unit


def test_initial_state() -> None:
    """A fresh tracker reports as idle with a small last-activity timestamp."""
    tracker = ActivityTracker()
    assert tracker.is_idle is True
    assert tracker.seconds_since_activity < 0.5


def test_connection_made_increments_active_count() -> None:
    """After one connection_made the tracker is no longer idle."""
    tracker = ActivityTracker()
    tracker.connection_made("127.0.0.1", 12345)
    assert tracker.is_idle is False


def test_connection_lost_decrements_active_count() -> None:
    """One made plus one lost returns the tracker to idle."""
    tracker = ActivityTracker()
    tracker.connection_made("127.0.0.1", 12345)
    tracker.connection_lost("127.0.0.1", 12345, None)
    assert tracker.is_idle is True


def test_multiple_concurrent_connections() -> None:
    """Multiple opens without close leave the tracker busy."""
    tracker = ActivityTracker()
    tracker.connection_made("h", 1)
    tracker.connection_made("h", 2)
    tracker.connection_made("h", 3)
    tracker.connection_lost("h", 1, None)
    assert tracker.is_idle is False  # 2 still active


def test_underflow_is_clamped_to_zero() -> None:
    """connection_lost without prior connection_made does not go negative."""
    tracker = ActivityTracker()
    tracker.connection_lost("h", 1, None)
    tracker.connection_lost("h", 2, None)
    assert tracker.is_idle is True
    tracker.connection_made("h", 3)
    tracker.connection_lost("h", 3, None)
    assert tracker.is_idle is True


def test_last_activity_updates_on_made() -> None:
    """connection_made bumps the activity timestamp forward."""
    tracker = ActivityTracker()
    time.sleep(0.05)
    initial_age = tracker.seconds_since_activity
    assert initial_age >= 0.05
    tracker.connection_made("h", 1)
    assert tracker.seconds_since_activity < initial_age


def test_last_activity_updates_on_lost() -> None:
    """connection_lost bumps the activity timestamp forward."""
    tracker = ActivityTracker()
    tracker.connection_made("h", 1)
    time.sleep(0.05)
    initial_age = tracker.seconds_since_activity
    tracker.connection_lost("h", 1, None)
    assert tracker.seconds_since_activity < initial_age


def test_connection_lost_accepts_exception_argument() -> None:
    """The exc parameter on connection_lost is accepted and ignored."""
    tracker = ActivityTracker()
    tracker.connection_made("h", 1)
    tracker.connection_lost("h", 1, ConnectionResetError("test"))
    assert tracker.is_idle is True
