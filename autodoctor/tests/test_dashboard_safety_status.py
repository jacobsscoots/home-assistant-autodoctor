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
        return False, "not allowed", None


def test_dashboard_separates_executor_enabled_from_automatic_repairs() -> None:
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[],
        plans=[],
        executor_health={"enabled": True},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "Approval gated" in text
    assert "Automatic repairs are always off" in text
