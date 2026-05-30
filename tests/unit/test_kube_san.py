"""TLS server-name selection from a certificate's SAN list, and SAN parsing.

Validates: prefer the original server host; else first DNS SAN; else
first IP SAN; empty SAN returns None. A non-exact match is flagged.
Also validates sans_from_cert for malformed DER, absent SAN extension,
and the normal DNS+IP extraction path.
Code: garuda_tunnel/kube.py
Assertion: choose_tls_server_name returns the documented preference and
a `fellback` flag indicating a non-exact match; sans_from_cert returns
([], []) on failure and (dns, ips) on success.
Method: call functions with crafted inputs and DER-encoded certificates.
"""

from __future__ import annotations

import datetime
import ipaddress

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from garuda_tunnel.kube import choose_tls_server_name, sans_from_cert

pytestmark = pytest.mark.unit


def test_prefers_original_host_when_in_san() -> None:
    """When the original server host is in SAN, it is chosen, no fallback."""
    name, fellback = choose_tls_server_name(
        original_host="am.prod.kube.example.net",
        dns_sans=["am.prod.kube.example.net", "kubernetes"],
        ip_sans=["192.0.2.40"],
    )
    assert name == "am.prod.kube.example.net"
    assert fellback is False


def test_falls_back_to_first_dns_san() -> None:
    """When the original host is absent, the first DNS SAN is chosen (fallback)."""
    name, fellback = choose_tls_server_name(
        original_host="127.0.0.1",
        dns_sans=["kubernetes", "kubernetes.default"],
        ip_sans=["192.0.2.40"],
    )
    assert name == "kubernetes"
    assert fellback is True


def test_falls_back_to_first_ip_san() -> None:
    """With no DNS SAN, the first IP SAN is chosen (fallback)."""
    name, fellback = choose_tls_server_name(
        original_host="127.0.0.1",
        dns_sans=[],
        ip_sans=["192.0.2.40", "192.0.2.50"],
    )
    assert name == "192.0.2.40"
    assert fellback is True


def test_prefers_original_host_when_in_ip_san() -> None:
    """Original host matches an IP SAN entry → chosen, no fallback (mirrors DNS case)."""
    name, fellback = choose_tls_server_name(
        original_host="192.0.2.40",
        dns_sans=["kubernetes"],
        ip_sans=["192.0.2.40", "127.0.0.1"],
    )
    assert name == "192.0.2.40"
    assert fellback is False


def test_empty_san_returns_none() -> None:
    """An empty SAN list returns None (caller decides insecure/fail)."""
    name, fellback = choose_tls_server_name(original_host="127.0.0.1", dns_sans=[], ip_sans=[])
    assert name is None
    assert fellback is True


# ---------------------------------------------------------------------------
# sans_from_cert
# ---------------------------------------------------------------------------


def _make_cert(*, add_san: bool) -> bytes:
    """Build a minimal self-signed DER certificate, optionally with a SAN."""
    key = ed25519.Ed25519PrivateKey.generate()
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2030, 1, 1))
    )
    if add_san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("dev-kube-1"),
                    x509.IPAddress(ipaddress.ip_address("192.0.2.11")),
                ]
            ),
            critical=False,
        )
    return builder.sign(key, None).public_bytes(serialization.Encoding.DER)


def test_sans_from_cert_malformed_returns_empty() -> None:
    """Malformed DER bytes yield ([], []) — not a crash."""
    assert sans_from_cert(b"not-a-cert") == ([], [])


def test_sans_from_cert_no_san_returns_empty() -> None:
    """A cert with no SAN extension yields ([], [])."""
    assert sans_from_cert(_make_cert(add_san=False)) == ([], [])


def test_sans_from_cert_extracts_dns_and_ip() -> None:
    """A cert with DNS+IP SANs returns them split into (dns, ip)."""
    dns, ips = sans_from_cert(_make_cert(add_san=True))
    assert dns == ["dev-kube-1"]
    assert ips == ["192.0.2.11"]
