"""Tunnel orchestration on asyncssh: per-node connection + forwards + SFTP."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import asyncssh

from garuda_tunnel.activity import ActivityTracker
from garuda_tunnel.exceptions import RequiredTunnelFailure, TunnelStartupError
from garuda_tunnel.fetcher import fetch_files
from garuda_tunnel.schemas import (
    ErrorOutput,
    FetchedFile,
    InputSchema,
    NodeOutput,
    OutputSchema,
    TunnelWarning,
)
from garuda_tunnel.ssh import close_transport, open_connection, open_local_forwards

# Errors we expect from a remote SSH peer, local sshd handshake, or
# from loading inline client material. Anything outside this tuple is a
# program bug and must propagate, not be flattened into a node failure.
_NODE_STARTUP_ERRORS: tuple[type[BaseException], ...] = (
    asyncssh.Error,
    asyncssh.KeyImportError,
    OSError,
    asyncio.TimeoutError,
    TunnelStartupError,
)


@dataclass
class _NodeRuntime:
    """Per-node bookkeeping shared between start_all and stop_all."""

    name: str
    success: bool
    ports: dict[str, int] = field(default_factory=dict)
    fetched_files: dict[str, FetchedFile] = field(default_factory=dict)
    conn: asyncssh.SSHClientConnection | None = None
    listeners: list[asyncssh.SSHListener] = field(default_factory=list)
    error: str | None = None


class TunnelManager:
    """Orchestrate asyncssh transports + fetch_files for an InputSchema."""

    def __init__(self, schema: InputSchema) -> None:
        """Store the parsed input schema; do not open any transport yet."""
        self._schema = schema
        self._runtimes: list[_NodeRuntime] = []
        self.activity_tracker = ActivityTracker()

    async def stop_all(self) -> None:
        """Close every active listener and connection, best-effort."""
        runtimes = list(self._runtimes)
        self._runtimes.clear()
        for runtime in runtimes:
            await close_transport(runtime.conn, runtime.listeners)

    async def start_all_and_build_output(
        self,
        *,
        pid: int,
        token: str,
    ) -> OutputSchema | ErrorOutput:
        """Open every node concurrently; build OutputSchema or ErrorOutput."""
        results = await self._start_all()
        failed_required = [
            r for r in results if not r.success and self._schema.nodes[r.name].required
        ]
        if failed_required:
            await self.stop_all()
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

        connections: dict[str, NodeOutput] = {
            r.name: NodeOutput(
                ports=r.ports,
                fetch_files=r.fetched_files,
            )
            for r in results
            if r.success
        }
        warnings = [
            TunnelWarning(node=r.name, error=r.error or "unknown error")
            for r in results
            if not r.success and not self._schema.nodes[r.name].required
        ]
        return OutputSchema(
            connections=connections,
            pid=pid,
            token=token,
            started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            warnings=warnings,
        )

    async def _start_all(self) -> list[_NodeRuntime]:
        """Start every node in the schema concurrently and return their runtimes."""
        names = list(self._schema.nodes.keys())
        coros = [self._start_one(name) for name in names]
        return await asyncio.gather(*coros)

    async def _start_one(self, name: str) -> _NodeRuntime:
        """Open one node end-to-end: connection, local forwards, optional fetch."""
        node = self._schema.nodes[name]
        runtime = _NodeRuntime(name=name, success=False)
        try:
            runtime.conn = await open_connection(node)
        except _NODE_STARTUP_ERRORS as exc:
            runtime.error = str(exc)
            return runtime

        try:
            entries, listeners = await open_local_forwards(
                runtime.conn, node, tracker=self.activity_tracker
            )
        except _NODE_STARTUP_ERRORS as exc:
            runtime.error = str(exc)
            await close_transport(runtime.conn, [])
            runtime.conn = None
            return runtime
        runtime.ports = entries
        runtime.listeners = listeners

        if node.fetch_files:
            try:
                fetched, required_failures = await fetch_files(runtime.conn, node.fetch_files)
            except _NODE_STARTUP_ERRORS as exc:
                runtime.error = str(exc)
                await close_transport(runtime.conn, runtime.listeners)
                runtime.conn = None
                runtime.listeners = []
                return runtime
            runtime.fetched_files = fetched
            if required_failures:
                runtime.error = f"required fetch_files failed: {required_failures}"
                await close_transport(runtime.conn, runtime.listeners)
                runtime.conn = None
                runtime.listeners = []
                return runtime

        runtime.success = True
        self._runtimes.append(runtime)
        return runtime
