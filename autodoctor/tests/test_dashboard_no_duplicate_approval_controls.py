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


def test_each_proposed_plan_has_one_approve_and_one_reject_control() -> None:
    plan = {
        "plan_id": "plan1",
        "status": "proposed",
        "risk": "low",
        "confidence": 0.95,
        "repair_type": "reload_config_entry",
        "summary": "Reload once",
    }
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[],
        plans=[plan],
        executor_health={"enabled": True},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert text.count("Approve one config-entry reload") == 1
    assert text.count("Reject plan") == 1
