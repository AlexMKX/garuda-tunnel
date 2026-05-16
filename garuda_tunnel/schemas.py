"""Pydantic models for CLI input/output. Single source of JSON shape."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class SSHOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compression: bool = False
    host_key_policy: Literal["auto", "reject", "warning"] = "auto"
    known_hosts_path: str | None = None
    connect_timeout: int = 60
    threaded: bool = True


class DaemonOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_file: str | None = None
    shutdown_grace_seconds: int = 10


class NodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 22
    user: str
    ssh_pkey: str | None = None
    ssh_password: str | None = None
    ssh_pkey_passphrase: str | None = None
    remote_ports: list[int] = Field(min_length=1)
    local_ports: list[int] | None = None
    ssh_options: SSHOptions = Field(default_factory=SSHOptions)


class InputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: dict[str, NodeInput]
    require: Literal["*"] | list[str] = "*"
    daemon: DaemonOptions = Field(default_factory=DaemonOptions)

    @field_validator("nodes")
    @classmethod
    def _validate_auth(cls, value: dict[str, NodeInput]) -> dict[str, NodeInput]:
        for name, node in value.items():
            if not node.ssh_pkey and not node.ssh_password:
                raise ValueError(f"node {name!r}: must provide ssh_pkey or ssh_password")
        return value

    @field_validator("require")
    @classmethod
    def _validate_require(
        cls,
        value: Literal["*"] | list[str],
        info: ValidationInfo,
    ) -> Literal["*"] | list[str]:
        if value == "*":
            return value
        nodes: dict[str, Any] = info.data.get("nodes", {})
        unknown = sorted(set(value) - set(nodes.keys()))
        if unknown:
            raise ValueError(f"require references unknown nodes: {unknown}")
        return value


class ConnectionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_host: str
    remote_port: int
    local_host: str
    local_port: int


class TunnelWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    error: str
    skipped: bool = True


class OutputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connections: dict[str, list[ConnectionEntry]]
    pid: int
    token: str
    started_at: str
    warnings: list[TunnelWarning] = Field(default_factory=list)


class ErrorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
