from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.models import LogEvent
from autodoctor.nonfatal import nonfatal_observation_reason


def _event(*, name: str, source: str) -> LogEvent:
    return LogEvent(
        level="ERROR",
        name=name,
        source=source,
        message="transport sub-call failed",
        exception="",
        timestamp=1.0,
    )


def test_raw_kasa_from_tplink_update_coordinator_is_observational() -> None:
    event = _event(
        name="kasa.smart.smartdevice",
        source="['components/tplink/coordinator.py', 78]",
    )
    reason = nonfatal_observation_reason(event, "kasa")
    assert reason is not None
    assert "correlation evidence" in reason


def test_windows_style_source_path_is_normalized() -> None:
    event = _event(
        name="kasa.protocol",
        source=r"components\tplink\coordinator.py:78",
    )
    assert nonfatal_observation_reason(event, "kasa") is not None


def test_real_home_assistant_tplink_coordinator_failure_is_not_suppressed() -> None:
    event = _event(
        name="homeassistant.components.tplink.coordinator",
        source="helpers/update_coordinator.py:506",
    )
    assert nonfatal_observation_reason(event, "tplink") is None


def test_kasa_from_other_call_path_remains_actionable() -> None:
    event = _event(
        name="kasa.discover",
        source="components/tplink/__init__.py:95",
    )
    assert nonfatal_observation_reason(event, "kasa") is None


def test_unrelated_library_cannot_match_by_source_path_alone() -> None:
    event = _event(
        name="aiohttp.client",
        source="components/tplink/coordinator.py:78",
    )
    assert nonfatal_observation_reason(event, "aiohttp") is None
