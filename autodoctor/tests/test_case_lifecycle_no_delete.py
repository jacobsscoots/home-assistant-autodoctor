from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager


def test_lifecycle_manager_never_deletes_incident_case_rows() -> None:
    source = inspect.getsource(LifecycleIncidentCaseManager)
    assert "DELETE FROM incident_cases" not in source
