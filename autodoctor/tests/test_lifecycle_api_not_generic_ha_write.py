from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.repair_dashboard import RepairDashboard


def test_manual_resolve_handler_does_not_call_home_assistant_services() -> None:
    source = inspect.getsource(RepairDashboard.resolve_case)
    assert "mark_resolved" in source
    assert "reload_config_entry" not in source
    assert "services" not in source
    assert "self.executor.approve_and_execute" not in source
