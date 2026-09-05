from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.models import LogEvent
from autodoctor.nonfatal import nonfatal_observation_reason


def test_nonfatal_reason_contains_no_event_private_values() -> None:
    private_marker = "private-device-marker"
    event = LogEvent(
        level="ERROR",
        source="components/tplink/coordinator.py:78",
        exception=private_marker,
        message=f"failed for {private_marker}",
        name="kasa.protocol",
        timestamp=1.0,
    )
    reason = nonfatal_observation_reason(event, "kasa")
    assert reason is not None
    assert private_marker not in reason
