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
        "ERROR",
        "kasa.protocol",
        "components/tplink/coordinator.py:78",
        f"failed for {private_marker}",
        private_marker,
        1.0,
    )
    reason = nonfatal_observation_reason(event, "kasa")
    assert reason is not None
    assert private_marker not in reason
