from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.models import LogEvent
from autodoctor.nonfatal import nonfatal_observation_reason


def test_kasa_family_is_not_globally_suppressed() -> None:
    event = LogEvent(
        "ERROR",
        "kasa.protocol",
        "kasa/protocols/klaptransport.py:300",
        "actual transport failure",
        "",
        1.0,
    )
    assert nonfatal_observation_reason(event, "kasa") is None
