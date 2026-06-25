"""Unit tests for the activity tracker used by idle-based auto-stop."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import asyncssh
import pytest

from tunstrap.activity import ActivityTracker

pytestmark = pytest.mark.unit


def _forwarder() -> MagicMock:
    """Return a minimal SSHForwarder mock accepted by connection_made."""
    return MagicMock(spec=asyncssh.SSHForwarder)


def test_initial_state() -> None:
    """A fresh aggregate reports idle with a small last-activity timestamp."""
    agg = ActivityTracker()
    assert agg.is_idle is True
    assert agg.seconds_since_activity < 0.5


def test_make_tracker_returns_idle_connection_tracker() -> None:
    """make_tracker() yields an SSHPortForwardTracker."""
    agg = ActivityTracker()
    ct = agg.make_tracker()
    assert isinstance(ct, asyncssh.SSHPortForwardTracker)


def test_connection_made_marks_aggregate_busy() -> None:
    """After connection_made the aggregate is no longer idle."""
    agg = ActivityTracker()
    ct = agg.make_tracker()
    ct.connection_made(_forwarder(), "127.0.0.1", 12345)
    assert agg.is_idle is False


def test_connection_lost_returns_aggregate_to_idle() -> None:
    """One made followed by one lost returns the aggregate to idle."""
    agg = ActivityTracker()
    ct = agg.make_tracker()
    ct.connection_made(_forwarder(), "h", 1)
    ct.connection_lost(None)
    assert agg.is_idle is True


def test_multiple_concurrent_connections() -> None:
    """Multiple opens without matching closes leave the aggregate busy."""
    agg = ActivityTracker()
    ct1 = agg.make_tracker()
    ct2 = agg.make_tracker()
    ct3 = agg.make_tracker()
    ct1.connection_made(_forwarder(), "h", 1)
    ct2.connection_made(_forwarder(), "h", 2)
    ct3.connection_made(_forwarder(), "h", 3)
    ct1.connection_lost(None)
    assert agg.is_idle is False  # ct2 and ct3 still active


def test_two_trackers_share_aggregate_state() -> None:
    """Trackers from the same aggregate share counters: one up, other down -> idle."""
    agg = ActivityTracker()
    ct1 = agg.make_tracker()
    ct2 = agg.make_tracker()
    ct1.connection_made(_forwarder(), "h", 1)
    ct2.connection_made(_forwarder(), "h", 2)
    assert agg.is_idle is False
    ct1.connection_lost(None)
    assert agg.is_idle is False
    ct2.connection_lost(None)
    assert agg.is_idle is True


def test_underflow_is_clamped_to_zero() -> None:
    """Extra connection_lost calls without prior made do not go negative."""
    agg = ActivityTracker()
    ct = agg.make_tracker()
    ct.connection_lost(None)
    ct.connection_lost(None)
    assert agg.is_idle is True
    ct.connection_made(_forwarder(), "h", 3)
    ct.connection_lost(None)
    assert agg.is_idle is True


def test_seconds_since_activity_updates_on_made() -> None:
    """connection_made bumps the last-activity timestamp forward."""
    agg = ActivityTracker()
    time.sleep(0.05)
    initial_age = agg.seconds_since_activity
    assert initial_age >= 0.05
    ct = agg.make_tracker()
    ct.connection_made(_forwarder(), "h", 1)
    assert agg.seconds_since_activity < initial_age


def test_seconds_since_activity_updates_on_lost() -> None:
    """connection_lost bumps the last-activity timestamp forward."""
    agg = ActivityTracker()
    ct = agg.make_tracker()
    ct.connection_made(_forwarder(), "h", 1)
    time.sleep(0.05)
    initial_age = agg.seconds_since_activity
    ct.connection_lost(None)
    assert agg.seconds_since_activity < initial_age


def test_connection_lost_accepts_exception_argument() -> None:
    """connection_lost(exc) is accepted and the exc is silently ignored."""
    agg = ActivityTracker()
    ct = agg.make_tracker()
    ct.connection_made(_forwarder(), "h", 1)
    ct.connection_lost(ConnectionResetError("peer reset"))
    assert agg.is_idle is True


def test_make_tracker_returns_fresh_instance_each_call() -> None:
    """Each make_tracker() call yields a distinct object."""
    agg = ActivityTracker()
    assert agg.make_tracker() is not agg.make_tracker()
