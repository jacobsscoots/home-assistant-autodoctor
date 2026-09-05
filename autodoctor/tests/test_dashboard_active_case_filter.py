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


def test_inactive_cases_do_not_appear_in_active_case_section() -> None:
    cases = [
        {"pattern_key": "a", "pattern_label": "Resolved label", "status": "resolved"},
        {"pattern_key": "b", "pattern_label": "Historical label", "status": "historical"},
        {"pattern_key": "c", "pattern_label": "Observed label", "status": "suppressed_nonfatal"},
        {"pattern_key": "d", "pattern_label": "Active label", "status": "diagnosed"},
    ]
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=cases,
        plans=[],
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "Active label" in text
    assert "Resolved label" not in text
    assert "Historical label" not in text
    assert "Observed label" not in text
