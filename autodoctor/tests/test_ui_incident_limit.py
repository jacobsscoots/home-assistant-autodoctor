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


def test_dashboard_bounds_incident_evidence_rows() -> None:
    incidents = [
        {
            "last_seen": index,
            "occurrences": 1,
            "pattern_label": f"Pattern {index}",
            "name": "demo",
            "message": "failure",
        }
        for index in range(80)
    ]
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=incidents,
        cases=[],
        plans=[],
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "Pattern 0" in text
    assert "Pattern 49" in text
    assert "Pattern 50" not in text
