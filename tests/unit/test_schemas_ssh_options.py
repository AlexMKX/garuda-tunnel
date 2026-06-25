"""Pin SSHOptions surface area.

Validates: SSHOptions exposes only documented fields (compression,
connect_timeout); legacy paramiko/sshtunnel-era fields are rejected by
extra=forbid.
Code: tunstrap/schemas.py::SSHOptions
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tunstrap.schemas import SSHOptions

pytestmark = pytest.mark.unit


def test_ssh_options_default_shape() -> None:
    """SSHOptions defaults expose exactly the documented surface."""
    opts = SSHOptions()
    assert opts.compression is False
    assert opts.connect_timeout == 60
    # Surface check: only these two fields exist.
    assert set(SSHOptions.model_fields) == {"compression", "connect_timeout"}


@pytest.mark.parametrize(
    "field",
    ["host_key_policy", "known_hosts_path", "threaded"],
)
def test_ssh_options_rejects_removed_fields(field: str) -> None:
    """Each removed legacy field is rejected by extra=forbid."""
    payload = {"compression": False, "connect_timeout": 60, field: True}
    with pytest.raises(ValidationError) as excinfo:
        SSHOptions.model_validate(payload)
    assert field in str(excinfo.value)
