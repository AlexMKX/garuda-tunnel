"""Kube mode: parse a remote kubeconfig, choose a TLS server name, patch it.

One kube_target maps to exactly one cluster: the kubeconfig's
current-context. Other contexts/clusters are ignored and left byte-stable
in the patched output. The fetched kubeconfig is untrusted input: it is
parsed in ruamel round-trip/safe mode and parse failures become a typed
KubeParseError (never a daemon crash).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from garuda_tunnel.exceptions import KubeParseError

__all__ = ["KubeParseError", "KubeconfigView", "parse_kubeconfig"]


@dataclass
class KubeconfigView:
    """Extracted current-context view plus the live parsed document.

    `doc` is the round-trip ruamel document used later for in-place patching
    (comments/key order preserved). The scalar fields are the extracted
    current-context cluster/user material.
    """

    doc: object
    context_name: str
    cluster_name: str
    user_name: str
    server: str
    certificate_authority_data: str
    client_certificate_data: str
    client_key_data: str
    ignored_contexts: list[str] = field(default_factory=list)


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    return y


def parse_kubeconfig(raw: bytes) -> KubeconfigView:
    """Parse raw kubeconfig bytes and extract the current-context view.

    Raises KubeParseError on malformed YAML, missing current-context, or an
    unresolvable cluster/user reference.
    """
    try:
        doc = _yaml().load(io.BytesIO(raw))
    except YAMLError as exc:
        raise KubeParseError(f"kubeconfig is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise KubeParseError("kubeconfig root is not a mapping")

    current = doc.get("current-context")
    if not current or not isinstance(current, str):
        raise KubeParseError("kubeconfig has no current-context")

    contexts = doc.get("contexts") or []
    ctx = _find_named(contexts, current)
    if ctx is None:
        raise KubeParseError(f"current-context {current!r} not found in contexts")
    _ctx_body_raw = ctx.get("context")
    ctx_body: dict[str, object] = _ctx_body_raw if isinstance(_ctx_body_raw, dict) else {}
    _cluster_name_raw = ctx_body.get("cluster")
    _user_name_raw = ctx_body.get("user")
    if not _cluster_name_raw or not _user_name_raw:
        raise KubeParseError(f"context {current!r} missing cluster or user")
    if not isinstance(_cluster_name_raw, str) or not isinstance(_user_name_raw, str):
        raise KubeParseError(f"context {current!r} cluster or user is not a string")
    cluster_name: str = _cluster_name_raw
    user_name: str = _user_name_raw

    cluster = _find_named(doc.get("clusters") or [], cluster_name)
    if cluster is None:
        raise KubeParseError(f"cluster {cluster_name!r} not found")
    _cluster_body_raw = cluster.get("cluster")
    cluster_body: dict[str, object] = (
        _cluster_body_raw if isinstance(_cluster_body_raw, dict) else {}
    )
    server = cluster_body.get("server")
    if not server or not isinstance(server, str):
        raise KubeParseError(f"cluster {cluster_name!r} has no server")

    user = _find_named(doc.get("users") or [], user_name)
    if user is None:
        raise KubeParseError(f"user {user_name!r} not found")
    _user_body_raw = user.get("user")
    user_body: dict[str, object] = _user_body_raw if isinstance(_user_body_raw, dict) else {}

    ignored = [
        str(c.get("name")) for c in contexts if isinstance(c, dict) and c.get("name") != current
    ]

    return KubeconfigView(
        doc=doc,
        context_name=current,
        cluster_name=str(cluster_name),
        user_name=str(user_name),
        server=server,
        certificate_authority_data=_string_field(
            cluster_body, "certificate-authority-data", cluster_name
        ),
        client_certificate_data=_string_field(user_body, "client-certificate-data", user_name),
        client_key_data=_string_field(user_body, "client-key-data", user_name),
        ignored_contexts=ignored,
    )


def _find_named(items: object, name: str) -> dict[str, object] | None:
    """Return the first list entry whose 'name' equals `name`, else None."""
    if not isinstance(items, list):
        return None
    for entry in items:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _string_field(body: dict[str, object], field_name: str, owner: str) -> str:
    """Return ``body[field_name]`` if absent or a string; raise on wrong type.

    Empty/missing fields return ``""`` (kubeconfigs may legitimately omit CA in
    insecure mode). A present-but-non-string value is a malformed kubeconfig.
    """
    value = body.get(field_name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KubeParseError(f"{owner!r} {field_name} must be a string, got {type(value).__name__}")
    return value
