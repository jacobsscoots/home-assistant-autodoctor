from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

import autodoctor.case_lifecycle as lifecycle


def test_repair_sensitive_statuses_are_never_quiet_retired() -> None:
    assert lifecycle._QUIET_RETIRE_STATUSES == {"new", "diagnosed", "reopened"}
    assert {"investigating", "repair_available", "needs_user_action", "verifying"}.isdisjoint(
        lifecycle._QUIET_RETIRE_STATUSES
    )
