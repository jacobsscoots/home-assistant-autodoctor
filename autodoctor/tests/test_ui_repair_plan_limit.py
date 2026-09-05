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


def test_dashboard_only_renders_proposed_repair_plans() -> None:
    plans = [
        {"plan_id": "p1", "status": "proposed", "summary": "Proposed one"},
        {"plan_id": "p2", "status": "rejected", "summary": "Rejected one"},
        {"plan_id": "p3", "status": "succeeded", "summary": "Succeeded one"},
    ]
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[],
        plans=plans,
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "Proposed one" in text
    assert "Rejected one" not in text
    assert "Succeeded one" not in text
