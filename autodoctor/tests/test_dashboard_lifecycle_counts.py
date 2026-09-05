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


def test_dashboard_surfaces_resolved_historical_and_observed_counts() -> None:
    health = {
        "status": "healthy",
        "case_management": {"cases_by_status": {"resolved": 2, "historical": 3, "suppressed_nonfatal": 4}},
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
    assert "2 resolved · 3 historical · 4 observed-only" in text
