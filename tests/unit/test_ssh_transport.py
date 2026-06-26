"""Unit tests for ssh.open_local_forwards and ssh.open_connection kwargs."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from tunstrap.exceptions import TunnelStartupError
from tunstrap.schemas import InputSchema
from tunstrap.ssh import open_connection, open_local_forwards

pytestmark = pytest.mark.unit


def make_node(
    *,
    remote_targets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal NodeInput payload for tests."""
    return {
        "host": "127.0.0.1",
        "user": "tester",
        "ssh_pkey": "PEM",
        "remote_targets": remote_targets or {"p": "127.0.0.1:6443"},
    }


def _fake_listener(port: int) -> MagicMock:
    """Mock listener that reports a fixed port and supports close()/wait_closed()."""
    listener = MagicMock(spec=asyncssh.SSHListener)
    listener.get_port.return_value = port
    listener.close = MagicMock()
    listener.wait_closed = AsyncMock()
    return listener


@pytest.mark.asyncio
async def test_forward_called_with_target_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each handle drives one forward_local_port call with (target.host, target.port)."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": make_node(
                    remote_targets={
                        "kubeapi": "10.0.0.1:6443",
                        "prom": "10.0.0.2:9090",
                    }
                )
            }
        }
    )
    node = schema.nodes["a"]
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(side_effect=[_fake_listener(54321), _fake_listener(54322)])
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    ports, listeners = await open_local_forwards(conn, node)

    assert ports == {"kubeapi": 54321, "prom": 54322}
    assert len(listeners) == 2
    args_first = conn.forward_local_port.await_args_list[0].args
    assert args_first == ("127.0.0.1", 0, "10.0.0.1", 6443)
    args_second = conn.forward_local_port.await_args_list[1].args
    assert args_second == ("127.0.0.1", 0, "10.0.0.2", 9090)


@pytest.mark.asyncio
async def test_probe_failure_raises_tunnel_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the local probe fails, raise TunnelStartupError with handle in details."""
    schema = InputSchema.model_validate(
        {"nodes": {"a": make_node(remote_targets={"kubeapi": "10.0.0.1:6443"})}}
    )
    node = schema.nodes["a"]
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(return_value=_fake_listener(54321))
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: False)

    with pytest.raises(TunnelStartupError) as exc:
        await open_local_forwards(conn, node)
    assert "local forward did not accept connection" in str(exc.value)
    assert exc.value.details["handle"] == "kubeapi"
    assert exc.value.details["target"] == "10.0.0.1:6443"
    assert exc.value.details["local_port"] == 54321


@pytest.mark.asyncio
async def test_forward_failure_cleans_up_previous_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If forward_local_port fails mid-loop, previously opened listeners are closed."""
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": make_node(
                    remote_targets={
                        "ok": "10.0.0.1:6443",
                        "bad": "10.0.0.2:9090",
                    }
                )
            }
        }
    )
    node = schema.nodes["a"]
    first = _fake_listener(54321)
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(
        side_effect=[first, asyncssh.ChannelOpenError(1, "no route")]
    )
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    with pytest.raises(asyncssh.ChannelOpenError):
        await open_local_forwards(conn, node)
    first.close.assert_called_once()


@pytest.mark.asyncio
async def test_forward_local_port_receives_tracker_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open_local_forwards passes its tracker_factory kwarg through to asyncssh."""
    schema = InputSchema.model_validate(
        {"nodes": {"a": make_node(remote_targets={"p": "10.0.0.1:6443"})}}
    )
    node = schema.nodes["a"]

    received_tracker_factory: list[object] = []

    async def fake_forward_local_port(
        listen_host: str,
        listen_port: int,
        dest_host: str,
        dest_port: int,
        *,
        tracker_factory: object = None,
    ) -> MagicMock:
        received_tracker_factory.append(tracker_factory)
        return _fake_listener(54321)

    conn = MagicMock()
    conn.forward_local_port = AsyncMock(side_effect=fake_forward_local_port)
    monkeypatch.setattr("tunstrap.ssh._probe_local_port", lambda *_args, **_kw: True)

    sentinel = object()
    await open_local_forwards(conn, node, tracker_factory=sentinel)

    assert received_tracker_factory == [sentinel]


@pytest.mark.asyncio
async def test_open_connection_agent_fallback_omits_client_keys_and_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no pkey/password and SSH_AUTH_SOCK is set, open_connection must NOT pass
    client_keys or password — letting asyncssh discover the agent itself."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/dummy-agent.sock")
    schema = InputSchema.model_validate(
        {
            "nodes": {
                "a": {
                    "host": "127.0.0.1",
                    "user": "tester",
                    "remote_targets": {"p": "127.0.0.1:6443"},
                }
            }
        }
    )
    node = schema.nodes["a"]
    assert node.ssh_pkey is None
    assert node.ssh_password is None

    captured_kwargs: dict[str, Any] = {}

    async def fake_connect(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return MagicMock(spec=asyncssh.SSHClientConnection)

    with patch("tunstrap.ssh.asyncssh.connect", side_effect=fake_connect):
        await open_connection(node)

    assert "client_keys" not in captured_kwargs
    assert "password" not in captured_kwargs
