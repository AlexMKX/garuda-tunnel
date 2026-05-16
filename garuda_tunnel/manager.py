"""Tunnel orchestration: parallel SSHTunnelForwarder startup and teardown."""

from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from typing import Iterable

import paramiko
import sshtunnel

from garuda_tunnel.exceptions import (
    RequiredTunnelFailure,
    SchemaValidationError,
    TunnelStartupError,
)
from garuda_tunnel.schemas import (
    ConnectionEntry,
    ErrorOutput,
    InputSchema,
    NodeInput,
    OutputSchema,
    TunnelWarning,
)

_PARAMIKO_KEY_CLASSES: list[type[paramiko.PKey]] = [
    paramiko.Ed25519Key,
    paramiko.ECDSAKey,
    paramiko.RSAKey,
]

# DSSKey is still exposed on paramiko 4.x; include it last to keep behavior
# stable while the sshtunnel/paramiko 5.0 incompatibility is unresolved.
_dss_cls = getattr(paramiko, "DSSKey", None)
if _dss_cls is not None:
    _PARAMIKO_KEY_CLASSES.append(_dss_cls)


def load_inline_pkey(pem: str, passphrase: str | None) -> paramiko.PKey:
    """Parse a PEM string into a Paramiko key. Never writes the key to disk."""
    last_error: Exception | None = None
    for cls in _PARAMIKO_KEY_CLASSES:
        try:
            return cls.from_private_key(StringIO(pem), password=passphrase)
        except paramiko.SSHException as exc:
            last_error = exc
            continue
        except ValueError as exc:
            last_error = exc
            continue
    raise SchemaValidationError(
        "ssh_pkey could not be parsed by any supported Paramiko key class",
        {"last_error": str(last_error) if last_error else ""},
    )


@dataclass
class _StartResult:
    name: str
    success: bool
    connections: list[ConnectionEntry] = field(default_factory=list)
    forwarder: sshtunnel.SSHTunnelForwarder | None = None
    error: str | None = None


class TunnelManager:
    def __init__(self, schema: InputSchema) -> None:
        self._schema = schema
        self._forwarders: list[sshtunnel.SSHTunnelForwarder] = []
        self._lock = threading.Lock()

    def stop_all(self) -> None:
        with self._lock:
            forwarders = list(self._forwarders)
            self._forwarders.clear()
        for fwd in forwarders:
            try:
                fwd.stop(force=True)
            except Exception:  # noqa: BLE001 - we want best-effort cleanup
                continue

    def start_all_and_build_output(
        self,
        *,
        pid: int,
        token: str,
    ) -> OutputSchema | ErrorOutput:
        results = self._start_all()
        required = self._required_set(results)
        failed_required = [r for r in results if not r.success and r.name in required]
        if failed_required:
            self.stop_all()
            exc = RequiredTunnelFailure(
                "required tunnel(s) failed to start",
                {
                    "failed": [
                        {"node": r.name, "error": r.error or "unknown"} for r in failed_required
                    ],
                },
            )
            return ErrorOutput(
                error=type(exc).__name__,
                message=exc.message,
                details=exc.details,
            )
        connections: dict[str, list[ConnectionEntry]] = {
            r.name: r.connections for r in results if r.success
        }
        warnings = [
            TunnelWarning(node=r.name, error=r.error or "unknown error")
            for r in results
            if not r.success
        ]
        return OutputSchema(
            connections=connections,
            pid=pid,
            token=token,
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            warnings=warnings,
        )

    def _required_set(self, results: Iterable[_StartResult]) -> set[str]:
        if self._schema.require == "*":
            return {r.name for r in results}
        assert isinstance(self._schema.require, list)
        return set(self._schema.require)

    def _start_all(self) -> list[_StartResult]:
        names = list(self._schema.nodes.keys())
        max_workers = max(1, min(len(names), 10))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return list(pool.map(self._start_one, names))

    def _start_one(self, name: str) -> _StartResult:
        node = self._schema.nodes[name]
        try:
            forwarder = self._build_forwarder(node)
            forwarder.start()
        except Exception as exc:  # noqa: BLE001 - aggregate per-node failure
            return _StartResult(name=name, success=False, error=str(exc))
        try:
            entries = self._verify_and_collect(node, forwarder)
        except Exception as exc:  # noqa: BLE001 - aggregate per-node failure
            try:
                forwarder.stop(force=True)
            except Exception:  # noqa: BLE001
                pass
            return _StartResult(name=name, success=False, error=str(exc))
        with self._lock:
            self._forwarders.append(forwarder)
        return _StartResult(name=name, success=True, connections=entries, forwarder=forwarder)

    def _build_forwarder(self, node: NodeInput) -> sshtunnel.SSHTunnelForwarder:
        local_binds: list[tuple[str, int]] = []
        if node.local_ports is not None:
            if len(node.local_ports) != len(node.remote_ports):
                raise TunnelStartupError(
                    "local_ports must align with remote_ports when provided",
                    {"node_remote_ports": node.remote_ports},
                )
            local_binds = [("127.0.0.1", p) for p in node.local_ports]
        else:
            local_binds = [("127.0.0.1", 0) for _ in node.remote_ports]
        remote_binds = [("127.0.0.1", p) for p in node.remote_ports]
        kwargs: dict[str, object] = {
            "ssh_address_or_host": (node.host, node.port),
            "ssh_username": node.user,
            "remote_bind_addresses": remote_binds,
            "local_bind_addresses": local_binds,
            "compression": node.ssh_options.compression,
            "set_keepalive": 30.0,
        }
        if node.ssh_pkey:
            kwargs["ssh_pkey"] = load_inline_pkey(node.ssh_pkey, node.ssh_pkey_passphrase)
        if node.ssh_password:
            kwargs["ssh_password"] = node.ssh_password
        forwarder = sshtunnel.SSHTunnelForwarder(**kwargs)
        forwarder.daemon_forward_servers = True
        forwarder.daemon_transport = True
        return forwarder

    def _verify_and_collect(
        self,
        node: NodeInput,
        forwarder: sshtunnel.SSHTunnelForwarder,
    ) -> list[ConnectionEntry]:
        entries: list[ConnectionEntry] = []
        for remote_port, local_bind in zip(
            node.remote_ports, forwarder.local_bind_addresses, strict=True
        ):
            local_host, local_port = local_bind
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(node.ssh_options.connect_timeout)
                if probe.connect_ex((local_host, local_port)) != 0:
                    raise TunnelStartupError(
                        "local forward did not accept connection",
                        {"remote_port": remote_port, "local_port": local_port},
                    )
            entries.append(
                ConnectionEntry(
                    remote_host="127.0.0.1",
                    remote_port=remote_port,
                    local_host=local_host,
                    local_port=local_port,
                )
            )
        return entries
