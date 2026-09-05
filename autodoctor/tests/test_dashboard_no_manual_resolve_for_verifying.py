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


def test_verifying_case_has_no_manual_resolve_control() -> None:
    case = {
        "pattern_key": "demo/x",
        "pattern_label": "verifying",
        "family": "demo",
        "status": "verifying",
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
    assert "Mark resolved &amp; dismiss" not in text
