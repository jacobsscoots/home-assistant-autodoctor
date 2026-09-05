from __future__ import annotations

import importlib.util
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[2]
MATCH_PATH = ROOT / "custom_components" / "autodoctor_private_resolver" / "match.py"
INIT_PATH = ROOT / "custom_components" / "autodoctor_private_resolver" / "__init__.py"

_spec = importlib.util.spec_from_file_location("autodoctor_private_resolver_match", MATCH_PATH)
assert _spec is not None and _spec.loader is not None
_match = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_match)

normalize_private_ipv4 = _match.normalize_private_ipv4
entry_data_matches_host = _match.entry_data_matches_host


def test_normalize_private_ipv4_accepts_only_rfc1918_literals() -> None:
    assert normalize_private_ipv4("10.1.2.3") == "10.1.2.3"
    assert normalize_private_ipv4("172.16.0.1") == "172.16.0.1"
    assert normalize_private_ipv4("172.31.255.255") == "172.31.255.255"
    assert normalize_private_ipv4("192.168.50.9") == "192.168.50.9"

    for value in (
        "8.8.8.8",
        "127.0.0.1",
        "169.254.1.1",
        "172.32.0.1",
        "100.64.0.1",
        "fe80::1",
        "kasa-device.local",
        "192.168.1.2:9999",
        "not-an-address",
    ):
        assert normalize_private_ipv4(value) is None


def test_entry_data_match_is_exact_host_only() -> None:
    entry = {
        "host": "192.168.1.25",
        "username": "private-user",
        "password": "private-password",
    }
    assert entry_data_matches_host(entry, "192.168.1.25") is True
    assert entry_data_matches_host(entry, "192.168.1.26") is False
    assert entry_data_matches_host({"title": "192.168.1.25"}, "192.168.1.25") is False
    assert entry_data_matches_host({"host": "8.8.8.8"}, "8.8.8.8") is False
    assert entry_data_matches_host(None, "192.168.1.25") is False


def test_entry_data_match_accepts_home_assistant_read_only_mapping() -> None:
    entry = MappingProxyType(
        {
            "host": "192.168.1.25",
            "username": "private-user",
            "password": "private-password",
        }
    )
    assert entry_data_matches_host(entry, "192.168.1.25") is True
    assert entry_data_matches_host(entry, "192.168.1.26") is False


def test_websocket_component_remains_read_only_and_tplink_scoped() -> None:
    source = INIT_PATH.read_text()
    assert 'async_entries(_TPLINK_DOMAIN)' in source
    assert '_TPLINK_DOMAIN = "tplink"' in source
    assert '"domain": _TPLINK_DOMAIN' in source
    assert '"count": len(matches)' in source
    assert '"matches": matches' in source
    assert 'services.async_call' not in source
    assert 'async_update_entry' not in source
    assert 'async_reload' not in source
    assert 'ConfigEntry.data' in source
