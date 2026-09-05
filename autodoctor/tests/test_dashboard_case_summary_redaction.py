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


def test_case_summary_is_redacted_before_rendering() -> None:
    cases = [{
        "pattern_key": "demo/x",
        "pattern_label": "demo",
        "family": "demo",
        "status": "diagnosed",
        "last_seen": 1,
        "occurrences": 1,
        "fingerprint_count": 1,
        "summary": "Device failed at 192.168.1.55 token=private-token",
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
    assert "192.168.1.55" not in text
    assert "private-token" not in text
    assert "&lt;IP&gt;" in text
