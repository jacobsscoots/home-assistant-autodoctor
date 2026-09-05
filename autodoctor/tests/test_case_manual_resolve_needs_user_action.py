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


def test_needs_user_action_case_can_be_explicitly_marked_resolved_after_manual_fix() -> None:
    case = {
        "pattern_key": "demo/x",
        "pattern_label": "manual attention",
        "family": "demo",
        "status": "needs_user_action",
        "last_seen": 1,
        "occurrences": 1,
        "fingerprint_count": 1,
    }
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[case],
        plans=[],
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "Mark resolved &amp; dismiss" in text
