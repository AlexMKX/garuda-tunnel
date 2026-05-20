"""Fetcher error classification.

Validates: garuda_tunnel/fetcher.py::_classify_error maps SFTP/transport
exceptions to stable string labels used in OutputSchema.
Code: garuda_tunnel/fetcher.py
"""

from __future__ import annotations

import socket

import asyncssh
import pytest

from garuda_tunnel.fetcher import _classify_error

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1, "SSH_FX_EOF"),
        (2, "SSH_FX_NO_SUCH_FILE"),
        (3, "SSH_FX_PERMISSION_DENIED"),
        (4, "SSH_FX_FAILURE"),
        (5, "SSH_FX_BAD_MESSAGE"),
        (6, "SSH_FX_NO_CONNECTION"),
        (7, "SSH_FX_CONNECTION_LOST"),
        (8, "SSH_FX_OP_UNSUPPORTED"),
        (99, "SSH_FX_UNKNOWN"),
    ],
)
def test_classify_sftp_error(code: int, expected: str) -> None:
    """SFTPError numeric codes map to their canonical SSH_FX_* labels."""
    exc = asyncssh.SFTPError(code=code, reason="boom")
    assert _classify_error(exc) == expected


def test_classify_channel_open_error() -> None:
    """ChannelOpenError is labelled as 'ChannelOpenError'."""
    assert _classify_error(asyncssh.ChannelOpenError(2, "denied")) == "ChannelOpenError"


def test_classify_connection_reset() -> None:
    """ConnectionResetError maps to its class name."""
    assert _classify_error(ConnectionResetError()) == "ConnectionResetError"


def test_classify_timeout() -> None:
    """TimeoutError maps to its class name."""
    assert _classify_error(TimeoutError()) == "TimeoutError"


def test_classify_socket_error() -> None:
    """socket.error is collapsed to 'OSError'."""
    assert _classify_error(socket.error("x")) == "OSError"


def test_classify_runtime_error() -> None:
    """Generic RuntimeError keeps its class name as label."""
    assert _classify_error(RuntimeError("x")) == "RuntimeError"
