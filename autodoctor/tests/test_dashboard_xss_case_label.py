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


def test_case_labels_are_html_escaped() -> None:
    cases = [{
        "pattern_key": "demo/x",
        "pattern_label": '<img src=x onerror="alert(1)">',
        "family": "demo",
        "status": "diagnosed",
        "last_seen": 1,
        "occurrences": 1,
        "fingerprint_count": 1,
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
    assert '<img src=x onerror="alert(1)">' not in text
    assert "&lt;img" in text
