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


def test_dashboard_bounds_active_case_rendering() -> None:
    cases = [
        {
            "pattern_key": f"demo/{index}",
            "pattern_label": f"Case {index}",
            "family": "demo",
            "status": "diagnosed",
            "last_seen": index,
            "occurrences": 1,
            "fingerprint_count": 1,
        }
        for index in range(60)
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
    # Active cases are sorted newest-first and capped at 40 to keep ingress responsive.
    assert "Case 59" in text
    assert "Case 20" in text
    assert "Case 19" not in text
