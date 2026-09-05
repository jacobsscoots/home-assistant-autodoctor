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


def test_dashboard_copy_never_implies_autonomous_repair() -> None:
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[],
        plans=[],
        executor_health={"enabled": True},
        executor=Executor(),
        approval_nonce="nonce",
    ).lower()
    assert "automatic repairs are off" in text
    assert "nothing executes without your approval" in text
    assert "autonomous repair" not in text
