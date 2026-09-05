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


def test_active_case_card_does_not_dump_root_cause_field() -> None:
    cases = [{
        "pattern_key": "demo/x",
        "pattern_label": "demo",
        "family": "demo",
        "status": "diagnosed",
        "last_seen": 1,
        "occurrences": 1,
        "fingerprint_count": 1,
        "summary": "safe summary",
        "root_cause": "sensitive-root-cause-marker",
    }]
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=cases,
        plans=[],
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "safe summary" in text
    assert "sensitive-root-cause-marker" not in text
