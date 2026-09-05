from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.models import LogEvent
from autodoctor.nonfatal import nonfatal_observation_reason
from autodoctor.scheduler import incident_family


def test_real_tplink_coordinator_failure_remains_actionable_end_to_end_family_classification() -> None:
    event = LogEvent(
        level="ERROR",
        name="homeassistant.components.tplink.coordinator",
        source="helpers/update_coordinator.py:506",
        message="Error requesting device data: not found",
        exception="",
        timestamp=1.0,
    )
    family = incident_family(event.name, event.source)
    assert family == "homeassistant.components.tplink"
    assert nonfatal_observation_reason(event, family) is None
