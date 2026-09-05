from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.control_dashboard import ControlDashboard


class Store:
    async def list_recent(self, limit):
        _ = limit
        return []


class Cases:
    async def list_cases(self, limit):
        _ = limit
        return []

    async def list_repair_plans(self, limit):
        _ = limit
        return []


class Engine:
    cases = Cases()

    async def health(self):
        return {"status": "healthy", "case_management": {}, "ai_budget": {}, "mcp": {}}


class Executor:
    approval_nonce = "nonce"
    enabled = False

    async def health(self):
        return {"enabled": False}

    @staticmethod
    def validate_plan(plan):
        _ = plan
        return False, "disabled", None


def test_control_dashboard_disables_cache_and_referrer_leakage() -> None:
    async def run() -> None:
        dashboard = object.__new__(ControlDashboard)
        dashboard.store = Store()
        dashboard.engine = Engine()
        dashboard.executor = Executor()
        response = await dashboard.index(SimpleNamespace())
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"

    asyncio.run(run())
