from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from autodoctor.dashboard_ui import render_dashboard
from autodoctor.repair_dashboard import RepairDashboard


class Executor:
    enabled = True
    approval_nonce = "synthetic-nonce"

    @staticmethod
    def validate_plan(plan):
        return True, "validated", "synthetic-target"


@pytest.mark.parametrize("surface", ["current", "legacy"])
@pytest.mark.parametrize("repair_type,label,notice", [
    ("reload_config_entry", "Approve one config-entry reload", "before reloading one integration entry"),
    ("diagnostic_log_template", "Approve diagnostic automation edit", "saves the displayed automation template change"),
    ("script_json_base64", "Approve logger script edit and reload", "The editor rewrites scripts.yaml"),
    ("future_validated_type", "Approve validated repair", "validated repair type shown above"),
])
def test_approval_describes_the_actual_mutation(surface, repair_type, label, notice):
    executor = Executor()
    plan = {"plan_id": "plan_test", "status": "proposed", "risk": "low", "confidence": 0.99,
            "repair_type": repair_type, "summary": "Synthetic repair"}
    if surface == "legacy":
        # The renderer uses only executor; no database, app server or live HA needed.
        dashboard = SimpleNamespace(executor=executor, _plan_summary=RepairDashboard._plan_summary)
        text = RepairDashboard._plan_card(dashboard, plan)
    else:
        text = render_dashboard(health={"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}},
                                incidents=[], cases=[], plans=[plan], executor_health={"enabled": True},
                                executor=executor, approval_nonce=executor.approval_nonce)
    assert label in text
    assert notice in text
    assert "confirmed" in text.lower()
    assert 'value="synthetic-nonce"' in text
    assert './api/repair-plans/plan_test/approve' in text
    if repair_type != "reload_config_entry":
        assert "Approve one config-entry reload" not in text
