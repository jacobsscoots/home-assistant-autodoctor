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


def test_repair_cards_do_not_dump_private_evidence_payload() -> None:
    plans = [{
        "plan_id": "plan",
        "status": "proposed",
        "risk": "low",
        "confidence": 0.95,
        "repair_type": "reload_config_entry",
        "summary": "review",
        "evidence": {"entry_id": "private-entry-marker"},
    }]
    text = render_dashboard(
        health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
        incidents=[],
        cases=[],
        plans=plans,
        executor_health={"enabled": False},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "private-entry-marker" not in text
