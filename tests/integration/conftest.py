from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest


HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def started_daemons() -> list[tuple[int, str]]:
    """Mutable list of (pid, token) pairs produced by successful start calls."""
    return []


@pytest.fixture(scope="session")
def ssh_keypair() -> tuple[str, str]:
    keys_dir = HERE / "_keys"
    keys_dir.mkdir(exist_ok=True)
    priv_path = keys_dir / "id_test"
    pub_path = keys_dir / "id_test.pub"
    if not priv_path.exists():
        # Paramiko 4 dropped Ed25519Key.generate; use cryptography directly.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        priv_obj = Ed25519PrivateKey.generate()
        with priv_path.open("w") as fh:
            fh.write(
                priv_obj.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.OpenSSH,
                    encryption_algorithm=serialization.NoEncryption(),
                ).decode()
            )
        os.chmod(priv_path, 0o600)
        pub = (
            priv_obj.public_key()
            .public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )
        with pub_path.open("w") as fh:
            fh.write(pub + " test\n")
    return priv_path.read_text(), pub_path.read_text()


@pytest.fixture(scope="session")
def ssh_test_cluster(ssh_keypair: tuple[str, str]) -> Iterator[dict[str, Any]]:
    if sys.platform != "linux":
        pytest.skip("integration tests require Linux + Docker")
    compose_file = HERE / "docker-compose.yml"
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d", "--wait"],
        check=True,
    )
    try:
        services = ["sshd-a", "sshd-b", "sshd-c"]
        ports: dict[str, int] = {}
        for service in services:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "port", service, "2222"],
                capture_output=True,
                text=True,
                check=True,
            )
            host, port = result.stdout.strip().rsplit(":", 1)
            ports[service] = int(port)
        priv_pem, _pub = ssh_keypair
        yield {
            "ports": ports,
            "private_pem": priv_pem,
            "user": "tester",
            "host": "127.0.0.1",
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down", "-v"],
            check=False,
        )


def garuda_tunnel_start(stdin_payload: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run(
        ["garuda-tunnel", "start"],
        input=json.dumps(stdin_payload),
        text=True,
        capture_output=True,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "json": json.loads(completed.stdout) if completed.stdout.strip() else None,
    }
