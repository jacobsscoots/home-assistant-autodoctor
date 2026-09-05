from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager


def test_lifecycle_cleanup_can_only_dismiss_stored_autodoctor_case_ids() -> None:
    source = inspect.getsource(LifecycleIncidentCaseManager._dismiss_owned_notification)
    assert 'startswith("autodoctor_case_")' in source
    assert "dismiss_notification" in source
    assert "persistent_notification" not in source
