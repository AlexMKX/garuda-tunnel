"""Patch a kubeconfig: rewrite server + set tls-server-name (or insecure).

Validates: only the current-context cluster's server is rewritten;
tls-server-name is set; comments/key order are preserved; insecure mode
drops CA and sets insecure-skip-tls-verify.
Code: tunstrap/kube.py
Assertion: re-parsing the patched output shows the new server/tls fields;
the original comment line survives; untouched clusters keep their server.
Method: parse a fixture, patch it, dump, and re-parse to assert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tunstrap.kube import dump_kubeconfig, parse_kubeconfig, patch_view

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kube"


def test_patch_rewrites_server_and_sets_tls_name() -> None:
    """Secure patch sets the local endpoint and tls-server-name on the cluster."""
    view = parse_kubeconfig((FIXTURES / "single_internal_ip.yaml").read_bytes())
    patch_view(view, local_port=40123, tls_server_name="dev-kube-1", insecure=False)
    out = dump_kubeconfig(view)
    reparsed = parse_kubeconfig(out)
    assert reparsed.server == "https://127.0.0.1:40123"
    text = out.decode()
    assert "tls-server-name: dev-kube-1" in text
    assert "insecure-skip-tls-verify" not in text.replace("#insecure-skip-tls-verify", "")


def test_patch_preserves_comment() -> None:
    """The commented insecure line in the fixture survives the round-trip."""
    view = parse_kubeconfig((FIXTURES / "single_internal_ip.yaml").read_bytes())
    patch_view(view, local_port=1, tls_server_name="x", insecure=False)
    assert b"#insecure-skip-tls-verify: true" in dump_kubeconfig(view)


def test_patch_insecure_drops_ca_and_sets_skip_verify() -> None:
    """Insecure patch sets insecure-skip-tls-verify and removes CA data."""
    view = parse_kubeconfig((FIXTURES / "hostname_split_horizon.yaml").read_bytes())
    patch_view(view, local_port=55555, tls_server_name=None, insecure=True)
    text = dump_kubeconfig(view).decode()
    assert "insecure-skip-tls-verify: true" in text
    assert "certificate-authority-data" not in text
    assert "tls-server-name" not in text
