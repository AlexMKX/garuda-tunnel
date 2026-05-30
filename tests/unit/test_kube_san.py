"""TLS server-name selection from a certificate's SAN list.

Validates: prefer the original server host; else first DNS SAN; else
first IP SAN; empty SAN returns None. A non-exact match is flagged.
Code: garuda_tunnel/kube.py
Assertion: choose_tls_server_name returns the documented preference and
a `fellback` flag indicating a non-exact match.
Method: call choose_tls_server_name with crafted SAN lists.
"""

from __future__ import annotations

import pytest

from garuda_tunnel.kube import choose_tls_server_name

pytestmark = pytest.mark.unit


def test_prefers_original_host_when_in_san() -> None:
    """When the original server host is in SAN, it is chosen, no fallback."""
    name, fellback = choose_tls_server_name(
        original_host="am.prod.kube.example.net",
        dns_sans=["am.prod.kube.example.net", "kubernetes"],
        ip_sans=["10.0.0.40"],
    )
    assert name == "am.prod.kube.example.net"
    assert fellback is False


def test_falls_back_to_first_dns_san() -> None:
    """When the original host is absent, the first DNS SAN is chosen (fallback)."""
    name, fellback = choose_tls_server_name(
        original_host="127.0.0.1",
        dns_sans=["kubernetes", "kubernetes.default"],
        ip_sans=["10.0.0.40"],
    )
    assert name == "kubernetes"
    assert fellback is True


def test_falls_back_to_first_ip_san() -> None:
    """With no DNS SAN, the first IP SAN is chosen (fallback)."""
    name, fellback = choose_tls_server_name(
        original_host="127.0.0.1",
        dns_sans=[],
        ip_sans=["10.0.0.40", "127.0.0.1"],
    )
    assert name == "10.0.0.40"
    assert fellback is True


def test_empty_san_returns_none() -> None:
    """An empty SAN list returns None (caller decides insecure/fail)."""
    name, fellback = choose_tls_server_name(
        original_host="127.0.0.1", dns_sans=[], ip_sans=[]
    )
    assert name is None
    assert fellback is True
