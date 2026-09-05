from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[2]
MATCH_PATH = ROOT / "custom_components" / "autodoctor_private_resolver" / "match.py"

_spec = importlib.util.spec_from_file_location("resolver_match_contract", MATCH_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


def test_mapping_proxy_matches_abstract_mapping_contract() -> None:
    proxy = MappingProxyType({"host": "192.168.1.30"})
    assert isinstance(proxy, Mapping)
    assert _module.entry_data_matches_host(proxy, "192.168.1.30")
