from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.nonfatal import nonfatal_observation_reason


def test_nonfatal_policy_does_not_depend_on_private_device_identifiers() -> None:
    source = inspect.getsource(nonfatal_observation_reason).lower()
    for forbidden in ("entry_id", "device_id", "entity_id", "mac", "ip_address", "host"):
        assert forbidden not in source
