from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.audit_access import internal_addon_remote_allowed


@pytest.mark.parametrize(
    "remote",
    ["172.30.32.1", "172.30.33.254", "fd0c:ac1e:2100::1"],
)
def test_supervisor_bridge_addresses_are_eligible_for_token_validation(remote):
    assert internal_addon_remote_allowed(remote)


@pytest.mark.parametrize(
    "remote",
    [
        None,
        "",
        "not-an-address",
        "172.30.31.255",
        "172.30.34.0",
        "fd0c:ac1e:20ff::1",
        "fd0c:ac1e:2101::1",
        "::1",
        "::ffff:172.30.33.20",
    ],
)
def test_addresses_outside_supervisor_bridge_are_rejected(remote):
    assert not internal_addon_remote_allowed(remote)
