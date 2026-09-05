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


def test_dashboard_never_renders_mcp_auth_or_secret_paths_from_health() -> None:
    health = {
        "status": "healthy",
        "case_management": {},
        "ai_budget": {},
        "mcp": {
            "connected": True,
            "server_profile": "ha-mcp",
            "auth_mode": "secret-path",
            "url": "http://private-secret-path",
            "token": "super-secret-token",
        },
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
    assert "private-secret-path" not in text
    assert "super-secret-token" not in text
    assert "secret-path" not in text
