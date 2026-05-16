from __future__ import annotations

from io import StringIO

import paramiko
import pytest

from garuda_tunnel.exceptions import SchemaValidationError
from garuda_tunnel.manager import load_inline_pkey


def _generate_pem(key_type: str) -> str:
    if key_type == "ed25519":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        priv = Ed25519PrivateKey.generate()
        return priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    if key_type == "rsa":
        key = paramiko.RSAKey.generate(bits=2048)
    elif key_type == "ecdsa":
        key = paramiko.ECDSAKey.generate()
    else:
        raise AssertionError(key_type)
    buf = StringIO()
    key.write_private_key(buf)
    return buf.getvalue()


@pytest.mark.parametrize("key_type", ["ed25519", "rsa", "ecdsa"])
def test_load_inline_pkey_accepts_supported_formats(key_type: str) -> None:
    pem = _generate_pem(key_type)
    parsed = load_inline_pkey(pem, passphrase=None)
    assert parsed is not None
    assert parsed.get_name()


def test_load_inline_pkey_rejects_garbage() -> None:
    with pytest.raises(SchemaValidationError) as excinfo:
        load_inline_pkey("not a key", passphrase=None)
    out = excinfo.value.to_error_output()
    assert "ssh_pkey" not in out["details"]
