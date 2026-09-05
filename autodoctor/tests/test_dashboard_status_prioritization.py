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


def test_attention_cases_render_before_low_priority_diagnosed_cases() -> None:
    cases = [
        {"pattern_key": "d", "pattern_label": "Diagnosed last", "family": "demo", "status": "diagnosed", "last_seen": 100, "occurrences": 1, "fingerprint_count": 1},
        {"pattern_key": "r", "pattern_label": "Repair first", "family": "demo", "status": "repair_available", "last_seen": 1, "occurrences": 1, "fingerprint_count": 1},
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
    assert text.index("Repair first") < text.index("Diagnosed last")
