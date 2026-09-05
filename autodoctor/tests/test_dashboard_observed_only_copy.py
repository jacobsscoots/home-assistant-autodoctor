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


def test_observation_only_copy_explains_retention_without_ai_or_repair() -> None:
    health = {
        "status": "healthy",
        "case_management": {"cases_by_status": {"suppressed_nonfatal": 3}},
        "ai_budget": {},
        "mcp": {},
    }
    text = render_dashboard(
        health=health,
        incidents=[],
        cases=[],
        plans=[],
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "still retained as evidence but skips AI, notifications and repairs" in text
