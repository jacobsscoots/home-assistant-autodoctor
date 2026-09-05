from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.models import LogEvent
from autodoctor.nonfatal import nonfatal_observation_reason


def test_similarly_named_source_does_not_accidentally_match() -> None:
    event = LogEvent(
        "ERROR",
        "kasa.protocol",
        "custom_components/tplink/coordinator.py:78",
        "failure",
        "",
        1.0,
    )
    # The current rule intentionally keys on HA Core's components/tplink/coordinator.py
    # call path, not a custom component that merely contains similar text.
    assert nonfatal_observation_reason(event, "kasa") is None
