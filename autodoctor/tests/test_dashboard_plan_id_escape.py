from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.dashboard_ui import render_dashboard


class Executor:
    enabled = False

    @staticmethod
    def validate_plan(plan):
        _ = plan
        return False, "disabled", None


def test_plan_id_is_attribute_escaped_in_repair_form() -> None:
    plan = {
        "plan_id": 'plan\" onfocus=\"alert(1)',
        "status": "proposed",
        "risk": "low",
        "confidence": 0.95,
        "repair_type": "manual_review",
        "summary": "review",
    }
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[],
        plans=[plan],
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert 'plan&quot; onfocus=&quot;alert(1)' in text
    assert 'plan" onfocus="alert(1)' not in text
