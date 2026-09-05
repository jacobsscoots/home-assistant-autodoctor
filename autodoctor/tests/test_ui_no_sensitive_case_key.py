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


def test_pattern_key_is_only_embedded_for_explicit_manual_action() -> None:
    inactive = [{
        "pattern_key": "private-pattern-key",
        "pattern_label": "historical",
        "family": "demo",
        "status": "historical",
        "last_seen": 1,
        "occurrences": 1,
        "fingerprint_count": 1,
    }]
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=inactive,
        plans=[],
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "private-pattern-key" not in text
