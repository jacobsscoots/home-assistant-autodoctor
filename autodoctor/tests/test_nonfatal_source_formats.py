from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.models import LogEvent
from autodoctor.nonfatal import nonfatal_observation_reason


def _reason(source: str):
    return nonfatal_observation_reason(
        LogEvent(
            level="ERROR",
            source=source,
            exception="",
            message="failure",
            name="kasa.protocol",
            timestamp=1.0,
        ),
        "kasa",
    )


def test_known_home_assistant_source_encodings_match() -> None:
    assert _reason("components/tplink/coordinator.py:78") is not None
    assert _reason("['components/tplink/coordinator.py', 78]") is not None
    assert _reason(r"components\tplink\coordinator.py:78") is not None


def test_prefixed_similar_path_does_not_match() -> None:
    assert _reason("custom_components/tplink/coordinator.py:78") is None
    assert _reason("fakecomponents/tplink/coordinator.py:78") is None
