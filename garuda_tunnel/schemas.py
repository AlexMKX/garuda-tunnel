"""Pydantic models for CLI input/output. Single source of JSON shape."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_FETCH_FILES_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")


def _parse_host_port(value: str) -> tuple[str, int]:
    """Parse 'host:port' or '[ipv6]:port' into (host, port).

    Raises ValueError on any malformed input. The host is not validated
    against DNS rules; let the SSH server reject unresolvable names at
    connect time. The port is range-checked to 1..65535.
    """
    if value.startswith("["):
        # IPv6 path: [host]:port
        closing = value.find("]:")
        if closing == -1:
            raise ValueError("IPv6 target must use [host]:port form")
        host = value[1:closing]
        port_str = value[closing + 2 :]
    else:
        if "::" in value and value.count(":") > 1:
            raise ValueError("IPv6 target must use [host]:port form")
        if ":" not in value:
            raise ValueError("missing ':' in target")
        host, port_str = value.rsplit(":", 1)
    if not host:
        raise ValueError("empty host")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError("port must be 1..65535") from exc
    if port < 1 or port > 65535:
        raise ValueError("port must be 1..65535")
    return host, port


class SSHOptions(BaseModel):
    """SSH transport options reachable from public input.

    Only fields actually consumed by garuda_tunnel/ssh.py::open_connection
    and open_local_forwards. Anything else is dead config and rejected.
    """

    model_config = ConfigDict(extra="forbid")

    compression: bool = False
    connect_timeout: int = 60


class DaemonOptions(BaseModel):
    """Daemon-side knobs: optional log file and shutdown grace period."""

    model_config = ConfigDict(extra="forbid")

    log_file: str | None = None
    shutdown_grace_seconds: int = 10


class FileSpec(BaseModel):
    """A single file the daemon should read once at start."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    required: bool = Field(
        default=True,
        description=(
            "If false, a failure to fetch this file is recorded as "
            "FetchedFile.error and does not fail the node."
        ),
    )

    @field_validator("path")
    @classmethod
    def _validate_absolute(cls, value: str) -> str:
        if value.startswith("~"):
            raise ValueError("path must be literal (no '~' expansion)")
        if not value.startswith("/"):
            raise ValueError("path must be absolute (start with '/')")
        return value


class RemoteTarget(BaseModel):
    """Parsed host:port target. Stored on NodeInput after validation."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)


class NodeInput(BaseModel):
    """One SSH endpoint plus its local-forward and fetch-files requests."""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 22
    user: str
    ssh_pkey: str | None = None
    ssh_password: str | None = None
    ssh_pkey_passphrase: str | None = None
    remote_targets: dict[str, RemoteTarget]
    ssh_options: SSHOptions = Field(default_factory=SSHOptions)
    required: bool = Field(
        default=True,
        description=(
            "If false, a failure to start this node's tunnel or to fetch its "
            "required files is downgraded to a TunnelWarning instead of "
            "aborting `start`."
        ),
    )
    fetch_files: dict[str, FileSpec] | None = None

    @field_validator("remote_targets", mode="before")
    @classmethod
    def _validate_remote_targets(cls, value: object) -> dict[str, RemoteTarget]:
        if not isinstance(value, dict):
            raise ValueError("remote_targets must be a dict")
        if len(value) == 0:
            raise ValueError("remote_targets: at least 1 entry required")
        if len(value) > 16:
            raise ValueError("remote_targets: at most 16 entries per node")
        parsed: dict[str, RemoteTarget] = {}
        for handle, raw in value.items():
            if not isinstance(handle, str) or not _FETCH_FILES_KEY_RE.match(handle):
                raise ValueError(
                    f"remote_targets key {handle!r}: must match ^[a-zA-Z_][a-zA-Z0-9_-]*$"
                )
            if len(handle) > 64:
                raise ValueError(f"remote_targets key {handle!r}: max 64 chars")
            if isinstance(raw, RemoteTarget):
                parsed[handle] = raw
                continue
            if isinstance(raw, dict):
                try:
                    parsed[handle] = RemoteTarget.model_validate(raw)
                except ValidationError as exc:
                    raise ValueError(
                        f"remote_targets[{handle!r}]: invalid dict form: {exc}"
                    ) from exc
                continue
            if not isinstance(raw, str):
                raise ValueError(f"remote_targets[{handle!r}]: value must be a 'host:port' string")
            try:
                host, port = _parse_host_port(raw)
            except ValueError as exc:
                raise ValueError(f"remote_targets[{handle!r}]: {raw!r}: {exc}") from exc
            parsed[handle] = RemoteTarget(host=host, port=port)
        return parsed

    @field_validator("fetch_files")
    @classmethod
    def _validate_fetch_files(cls, value: dict[str, FileSpec] | None) -> dict[str, FileSpec] | None:
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("fetch_files: omit field instead of empty dict")
        if len(value) > 16:
            raise ValueError("fetch_files: at most 16 entries per node")
        for name in value:
            if len(name) > 64:
                raise ValueError(f"fetch_files key {name!r}: max 64 chars")
            if not _FETCH_FILES_KEY_RE.match(name):
                raise ValueError(f"fetch_files key {name!r}: must match ^[a-zA-Z_][a-zA-Z0-9_-]*$")
        return value


class InputSchema(BaseModel):
    """Top-level input read from stdin by ``garuda-tunnel start``."""

    model_config = ConfigDict(extra="forbid")

    nodes: dict[str, NodeInput]
    daemon: DaemonOptions = Field(default_factory=DaemonOptions)

    @field_validator("nodes")
    @classmethod
    def _validate_auth(cls, value: dict[str, NodeInput]) -> dict[str, NodeInput]:
        for name, node in value.items():
            if not node.ssh_pkey and not node.ssh_password:
                raise ValueError(f"node {name!r}: must provide ssh_pkey or ssh_password")
        return value


class FetchedFile(BaseModel):
    """Either a successful read (content_b64+size+sha256) or an error string."""

    model_config = ConfigDict(extra="forbid")

    content_b64: str | None = None
    size: int | None = None
    sha256: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_xor(self) -> "FetchedFile":
        has_success = self.content_b64 is not None
        has_error = self.error is not None
        if has_success and has_error:
            raise ValueError("FetchedFile: cannot set both content_b64 and error")
        if not has_success and not has_error:
            raise ValueError("FetchedFile: must set either content_b64 or error")
        if has_success and (self.size is None or self.sha256 is None):
            raise ValueError("FetchedFile: content_b64 requires size and sha256")
        if has_error and (self.size is not None or self.sha256 is not None):
            raise ValueError("FetchedFile: error branch must not set size/sha256")
        return self


class NodeOutput(BaseModel):
    """Per-node success payload: handle->local_port plus any fetched files."""

    model_config = ConfigDict(extra="forbid")

    ports: dict[str, int]
    fetch_files: dict[str, FetchedFile] = Field(default_factory=dict)


class TunnelWarning(BaseModel):
    """Non-fatal failure on an optional node, surfaced in the warnings array."""

    model_config = ConfigDict(extra="forbid")

    node: str
    error: str
    skipped: bool = True


class OutputSchema(BaseModel):
    """Success envelope returned by ``garuda-tunnel start`` on stdout."""

    model_config = ConfigDict(extra="forbid")

    connections: dict[str, NodeOutput]
    pid: int
    token: str
    started_at: str
    warnings: list[TunnelWarning] = Field(default_factory=list)


class ErrorOutput(BaseModel):
    """Error envelope returned by ``garuda-tunnel start`` on stdout."""

    model_config = ConfigDict(extra="forbid")

    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
