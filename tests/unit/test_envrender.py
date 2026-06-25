import pytest
from tunstrap.envrender import render_env, format_exports
from tunstrap.schemas import OutputSchema, NodeOutput, KubeTargetOutput


def _kube_out(port, path):
    return KubeTargetOutput(
        cluster_name="c",
        context_name="ctx",
        local_port=port,
        endpoint=f"https://127.0.0.1:{port}",
        tls_server_name="c",
        certificate_authority_data="",
        client_certificate_data="",
        client_key_data="",
        content_b64="",
        path=path,
    )


def test_render_ports_and_session():
    out = OutputSchema(
        connections={"h": NodeOutput(ports={"db-1": 5432})},
        pid=42,
        session_dir="/run/s",
        started_at="now",
    )
    env = render_env(out)
    assert env["TUNSTRAP_SESSION_DIR"] == "/run/s"
    assert env["TUNSTRAP_PID"] == "42"
    assert env["TUNSTRAP_DB_1_PORT"] == "5432"
    assert env["TUNSTRAP_DB_1_ENDPOINT"] == "127.0.0.1:5432"
    assert "KUBECONFIG" not in env


def test_render_kube_sets_kubeconfig():
    out = OutputSchema(
        connections={
            "h": NodeOutput(
                ports={}, kube_targets={"k3s": _kube_out(7000, "/run/s/tunnel-data/k3s")}
            )
        },
        pid=1,
        session_dir="/run/s",
        started_at="now",
    )
    env = render_env(out)
    assert env["TUNSTRAP_K3S_KUBECONFIG"] == "/run/s/tunnel-data/k3s"
    assert env["KUBECONFIG"] == "/run/s/tunnel-data/k3s"
    assert env["TUNSTRAP_K3S_ENDPOINT"] == "https://127.0.0.1:7000"


def test_render_kube_not_materialized_raises():
    out = OutputSchema(
        connections={"h": NodeOutput(ports={}, kube_targets={"k3s": _kube_out(7000, None)})},
        pid=1,
        session_dir="/run/s",
        started_at="now",
    )
    with pytest.raises(ValueError, match="not materialized"):
        render_env(out)


def test_render_requires_single_node():
    out = OutputSchema(connections={}, pid=1, session_dir="/s", started_at="now")
    with pytest.raises(ValueError, match="exactly one node"):
        render_env(out)


def test_format_exports_quotes_safely():
    txt = format_exports({"A": "x'y", "B": "z"})
    assert "export A='x'\\''y'" in txt
    assert "export B='z'" in txt
