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
        return False, "disabled", None


def test_dashboard_does_not_render_obsolete_executor_disabled_claim() -> None:
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[],
        plans=[],
        executor_health={"enabled": True},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "repair executor remains disabled" not in text.lower()
    assert "automatic repairs are off" in text.lower()
