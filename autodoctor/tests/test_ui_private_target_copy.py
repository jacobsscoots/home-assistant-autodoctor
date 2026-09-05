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


def test_dashboard_surfaces_private_target_boundary_without_identifiers() -> None:
    health = {
        "status": "healthy",
        "case_management": {
            "private_target_resolution": {
                "identifiers_exposed_to_ai": False,
                "bindings": 2,
                "withheld": 4,
                "last_result": "not_requested",
            }
        },
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
    assert "Private target identifiers are withheld from the AI" in text
    assert "entry_id" not in text
