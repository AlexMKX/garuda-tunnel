"""Server-URL parsing for kubeconfig clusters.

Validates: _split_host_port handles https URLs, IPv6, default port, and
rejects malformed/non-https URLs with KubeParseError.
Code: garuda_tunnel/kube.py::_split_host_port
Assertion: valid URLs return (host, port); malformed/non-https raise KubeParseError.
Method: call _split_host_port with crafted server strings.
"""

from __future__ import annotations

import pytest

from garuda_tunnel.exceptions import KubeParseError
from garuda_tunnel.kube import _split_host_port

pytestmark = pytest.mark.unit


def test_https_host_port() -> None:
    """A standard https URL yields (host, port)."""
    assert _split_host_port("https://192.0.2.11:6443") == ("192.0.2.11", 6443)


def test_https_default_port() -> None:
    """A URL without an explicit port defaults to 443."""
    assert _split_host_port("https://kube.example.net") == ("kube.example.net", 443)


def test_ipv6_bracketed() -> None:
    """An IPv6 bracketed host is parsed without brackets."""
    assert _split_host_port("https://[2001:db8::1]:6443") == ("2001:db8::1", 6443)


def test_url_with_path_and_query_does_not_break_port() -> None:
    """A query string with a colon does not corrupt port parsing (regression)."""
    assert _split_host_port("https://[2001:db8::1]:8443/foo?x=a:b") == ("2001:db8::1", 8443)


def test_rejects_non_https() -> None:
    """A non-https scheme is rejected."""
    with pytest.raises(KubeParseError):
        _split_host_port("http://192.0.2.11:6443")


def test_rejects_no_host() -> None:
    """A URL without a host is rejected."""
    with pytest.raises(KubeParseError):
        _split_host_port("https://")


def test_rejects_bad_port() -> None:
    """A non-numeric port is rejected as KubeParseError, not raw ValueError."""
    with pytest.raises(KubeParseError):
        _split_host_port("https://192.0.2.11:notaport")
