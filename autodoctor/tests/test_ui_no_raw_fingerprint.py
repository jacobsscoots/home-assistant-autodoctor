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


def test_redesigned_incident_table_does_not_render_raw_fingerprint_column() -> None:
    incident = {
        "fingerprint": "privatefingerprint123",
        "last_seen": 1,
        "occurrences": 1,
        "pattern_label": "demo",
        "name": "demo",
        "message": "failure",
    }
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[incident],
        cases=[],
        plans=[],
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "privatefingerprint123" not in text
