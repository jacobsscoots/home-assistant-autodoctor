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


def test_repair_card_states_ai_cannot_select_home_assistant_service() -> None:
    plan = {
        "plan_id": "plan1",
        "status": "proposed",
        "risk": "low",
        "confidence": 0.95,
        "repair_type": "reload_config_entry",
        "summary": "Safe reload",
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
    assert "The AI cannot call this endpoint or select another Home Assistant service" in text
