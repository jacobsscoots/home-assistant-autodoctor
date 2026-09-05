from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

import autodoctor.case_lifecycle as lifecycle


def test_active_and_inactive_notification_status_sets_are_disjoint() -> None:
    assert lifecycle._ACTIVE_NOTIFICATION_STATUSES.isdisjoint(
        lifecycle._INACTIVE_NOTIFICATION_STATUSES
    )
    assert "repair_available" in lifecycle._ACTIVE_NOTIFICATION_STATUSES
    assert "verifying" in lifecycle._ACTIVE_NOTIFICATION_STATUSES
    assert "resolved" in lifecycle._INACTIVE_NOTIFICATION_STATUSES
