from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.dashboard_ui import render_dashboard


class Executor:
    enabled = True

    @staticmethod
    def validate_plan(plan):
        _ = plan
        return True, "ok", "private"


def test_repair_summary_is_redacted_before_rendering() -> None:
    plans = [{
        "plan_id": "plan",
        "status": "proposed",
        "risk": "low",
        "confidence": 0.95,
        "repair_type": "reload_config_entry",
        "summary": "Reload device at 10.0.0.44 token=private-token",
    }]
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[],
        plans=plans,
        executor_health={"enabled": True},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "10.0.0.44" not in text
    assert "private-token" not in text
