from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.investigator import TargetedReadOnlyInvestigator
from autodoctor.models import LogEvent


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("sensor.room_2 and switch.PLUG_1", ["sensor.room_2", "switch.PLUG_1"]),
        ("sensor.café and light.台所", []),
        ("ésensor.room sensor.roomé", []),
        ("sensor.room sensor.room", ["sensor.room"]),
    ],
)
def test_entity_references_preserve_ascii_ids_and_unicode_boundaries(message, expected):
    event = LogEvent("ERROR", "", "", message, "", 0.0)
    assert TargetedReadOnlyInvestigator.referenced_entities(event) == expected
