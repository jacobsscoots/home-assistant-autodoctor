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


def test_repair_available_case_cannot_be_hidden_by_manual_resolve_button() -> None:
    cases = [{
        "pattern_key": "demo/x",
        "pattern_label": "repair case",
        "family": "demo",
        "status": "repair_available",
        "last_seen": 1,
        "occurrences": 1,
        "fingerprint_count": 1,
    }]
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=cases,
        plans=[],
        executor_health={"enabled": True},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "repair case" in text
    assert "Mark resolved &amp; dismiss" not in text
