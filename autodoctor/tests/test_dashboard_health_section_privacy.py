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


def test_dashboard_health_summary_does_not_dump_raw_health_json() -> None:
    secret = "should-never-render"
    health = {
        "status": "healthy",
        "case_management": {},
        "ai_budget": {},
        "mcp": {},
        "unexpected_secret_field": secret,
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
    assert secret not in text
