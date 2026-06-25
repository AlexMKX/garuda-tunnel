# tests/unit/test_cli_input.py
import pytest
from tunstrap.cli_input import parse_endpoint, parse_named, build_single_node_schema
from tunstrap.exceptions import SchemaValidationError
from tunstrap.schemas import DaemonOptions


def test_parse_endpoint_defaults_port():
    assert parse_endpoint("root@host") == ("root", "host", 22)


def test_parse_endpoint_explicit_port():
    assert parse_endpoint("u@h:2222") == ("u", "h", 2222)


def test_parse_endpoint_ipv6():
    assert parse_endpoint("u@[2001:db8::1]:6443") == ("u", "2001:db8::1", 6443)


def test_parse_endpoint_missing_user():
    with pytest.raises(SchemaValidationError):
        parse_endpoint("host:22")


def test_parse_endpoint_bad_port():
    with pytest.raises(SchemaValidationError):
        parse_endpoint("u@h:99999")


def test_parse_named_ok():
    assert parse_named(("api=127.0.0.1:6443",), "target") == {"api": "127.0.0.1:6443"}


def test_parse_named_missing_eq():
    with pytest.raises(SchemaValidationError):
        parse_named(("noeq",), "target")


def test_parse_named_dup():
    with pytest.raises(SchemaValidationError):
        parse_named(("a=1", "a=2"), "target")


def test_build_kube_only(tmp_path):
    key = tmp_path / "id"
    key.write_text("PEMDATA")
    schema = build_single_node_schema(
        connection="root@h:22",
        ssh_key=str(key),
        ssh_key_passphrase=None,
        ssh_password=None,
        targets=(),
        kube=("k3s=/etc/rancher/k3s/k3s.yaml",),
        fetch=(),
        daemon_opts=DaemonOptions(),
    )
    node = schema.nodes["node"]
    assert node.host == "h"
    assert node.user == "root" and node.port == 22
    assert node.ssh_pkey == "PEMDATA"
    assert node.remote_targets == {}
    assert node.kube_targets["k3s"].kubeconfig_path == "/etc/rancher/k3s/k3s.yaml"


def test_build_target_and_password():
    schema = build_single_node_schema(
        connection="u@h",
        ssh_key=None,
        ssh_key_passphrase=None,
        ssh_password="secret",
        targets=("db=127.0.0.1:5432",),
        kube=(),
        fetch=(),
        daemon_opts=DaemonOptions(),
    )
    node = schema.nodes["node"]
    assert node.ssh_password == "secret"
    assert node.remote_targets["db"].port == 5432


def test_build_ip_literal_host():
    """IP-literal hosts must work (node key is fixed 'node', not the host)."""
    schema = build_single_node_schema(
        connection="root@127.0.0.1:22",
        ssh_key=None,
        ssh_key_passphrase=None,
        ssh_password="pw",
        targets=("db=127.0.0.1:5432",),
        kube=(),
        fetch=(),
        daemon_opts=DaemonOptions(),
    )
    assert "node" in schema.nodes
    assert schema.nodes["node"].host == "127.0.0.1"
