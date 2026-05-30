"""KubeTargetOutput / NodeOutput.kube_targets / OutputSchema.session_dir.

Validates: the output models carry the extracted kube fields and the
always-present session_dir.
Code: garuda_tunnel/schemas.py
Assertion: a fully-populated KubeTargetOutput round-trips; NodeOutput
defaults kube_targets to {}; OutputSchema requires session_dir.
Method: construct models and assert field values / required errors.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from garuda_tunnel.schemas import (
    KubeTargetOutput,
    NodeOutput,
    OutputSchema,
)

pytestmark = pytest.mark.unit


def _kube_output(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cluster_name": "production",
        "context_name": "production",
        "local_port": 40123,
        "endpoint": "https://127.0.0.1:40123",
        "tls_server_name": "am.prod.kube.example.net",
        "certificate_authority_data": "Y2E=",
        "client_certificate_data": "Y2VydA==",
        "client_key_data": "a2V5",
        "content_b64": "a3ViZWNvbmZpZw==",
        "path": None,
    }
    base.update(overrides)
    return base


def test_kube_target_output_roundtrip() -> None:
    """A fully-populated KubeTargetOutput preserves all fields."""
    out = KubeTargetOutput.model_validate(_kube_output())
    assert out.endpoint == "https://127.0.0.1:40123"
    assert out.tls_server_name == "am.prod.kube.example.net"
    assert out.path is None


def test_kube_target_output_insecure_allows_empty_ca_and_null_tls() -> None:
    """Insecure fallback shape: empty CA and null tls_server_name are valid."""
    out = KubeTargetOutput.model_validate(
        _kube_output(certificate_authority_data="", tls_server_name=None)
    )
    assert out.certificate_authority_data == ""
    assert out.tls_server_name is None


def test_node_output_kube_targets_defaults_empty() -> None:
    """NodeOutput.kube_targets defaults to an empty dict."""
    node = NodeOutput.model_validate({"ports": {"p": 1}})
    assert node.kube_targets == {}


def test_output_schema_requires_session_dir() -> None:
    """OutputSchema.session_dir is required (always present)."""
    with pytest.raises(ValidationError):
        OutputSchema.model_validate(
            {"connections": {}, "pid": 1, "token": "t", "started_at": "now"}
        )


def test_output_schema_with_session_dir() -> None:
    """OutputSchema accepts a session_dir string."""
    schema = OutputSchema.model_validate(
        {
            "connections": {},
            "pid": 1,
            "token": "t",
            "started_at": "now",
            "session_dir": "/run/garuda/1",
        }
    )
    assert schema.session_dir == "/run/garuda/1"
