from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.cases import IncidentCaseManager


def test_case_notification_id_is_stable_and_namespaced() -> None:
    first = IncidentCaseManager.notification_id("family/pattern/abc")
    second = IncidentCaseManager.notification_id("family/pattern/abc")
    other = IncidentCaseManager.notification_id("family/pattern/def")
    assert first == second
    assert first.startswith("autodoctor_case_")
    assert first != other
