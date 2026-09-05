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


def test_dashboard_renders_safely_with_sparse_health_payload() -> None:
    text = render_dashboard(
        health={},
        incidents=[],
        cases=[],
        plans=[],
        executor_health={},
        executor=Executor(),
        approval_nonce="nonce",
    )
    assert "AutoDoctor" in text
    assert "Unknown" in text
