"""Parse a kubeconfig and extract current-context cluster + user material.

Validates: KubeconfigView extracts server/CA/cert/key for the current
context; multi-context files yield an ignored-contexts warning; a
malformed kubeconfig raises KubeParseError.
Code: garuda_tunnel/kube.py
Assertion: extracted fields match the fixtures; warnings list names the
ignored contexts; bad YAML raises KubeParseError (not a bare YAMLError).
Method: load fixtures from tests/unit/fixtures/kube and call parse_kubeconfig.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from garuda_tunnel.kube import KubeParseError, parse_kubeconfig

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kube"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_single_internal_ip() -> None:
    """current-context 'production' yields the internal-IP server + creds."""
    view = parse_kubeconfig(_read("single_internal_ip.yaml"))
    assert view.context_name == "production"
    assert view.cluster_name == "production"
    assert view.server == "https://10.0.0.11:6443"
    assert view.certificate_authority_data == "Y2EtZGF0YQ=="
    assert view.client_certificate_data == "Y2VydC1kYXRh"
    assert view.client_key_data == "a2V5LWRhdGE="
    assert view.ignored_contexts == []


def test_parse_hostname_server() -> None:
    """A hostname server is extracted verbatim from the current context."""
    view = parse_kubeconfig(_read("hostname_split_horizon.yaml"))
    assert view.cluster_name == "kubernetes"
    assert view.server == "https://am.prod.kube.example.net:6443"


def test_parse_multi_context_reports_ignored() -> None:
    """A multi-context file selects current-context and reports the others."""
    view = parse_kubeconfig(_read("multi_context.yaml"))
    assert view.context_name == "staging"
    assert view.server == "https://10.0.0.40:6443"
    assert "kubernetes-admin@kubernetes" in view.ignored_contexts


def test_parse_rejects_malformed_yaml() -> None:
    """Malformed YAML raises KubeParseError, not a bare YAMLError."""
    with pytest.raises(KubeParseError):
        parse_kubeconfig(b"clusters: [unterminated")


def test_parse_rejects_missing_current_context() -> None:
    """A kubeconfig without current-context raises KubeParseError."""
    with pytest.raises(KubeParseError):
        parse_kubeconfig(b"apiVersion: v1\nkind: Config\nclusters: []\n")
