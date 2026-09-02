from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.control_dashboard import ControlDashboard
from autodoctor.startup_recovery import recover_interrupted_case_investigations


class FakeCases:
    def __init__(self) -> None:
        self.rows = {
            "p-investigating": {"pattern_key": "p-investigating", "status": "investigating"},
            "p-new": {"pattern_key": "p-new", "status": "new"},
            "p-repair": {"pattern_key": "p-repair", "status": "repair_available"},
        }
        self.published: list[tuple[str, bool]] = []

    async def list_cases(self, limit: int = 500):
        _ = limit
        return [dict(row) for row in self.rows.values()]

    async def _set_status(self, pattern_key: str, status: str) -> None:
        self.rows[pattern_key]["status"] = status

    async def publish_case(self, pattern_key: str, *, force: bool = False) -> bool:
        self.published.append((pattern_key, force))
        return True


class FakeEngine:
    def __init__(self) -> None:
        self.cases = FakeCases()


def test_startup_recovery_reopens_only_interrupted_investigations() -> None:
    async def run() -> None:
        engine = FakeEngine()
        recovered = await recover_interrupted_case_investigations(engine)
        assert recovered == 1
        assert engine.cases.rows["p-investigating"]["status"] == "reopened"
        assert engine.cases.rows["p-new"]["status"] == "new"
        assert engine.cases.rows["p-repair"]["status"] == "repair_available"
        assert engine.cases.published == [("p-investigating", True)]

        second = await recover_interrupted_case_investigations(engine)
        assert second == 0

    asyncio.run(run())


def test_backlog_dashboard_card_surfaces_worker_progress() -> None:
    card = ControlDashboard._triage_card(
        {
            "case_management": {
                "cases_by_status": {
                    "new": 9,
                    "investigating": 1,
                    "diagnosed": 3,
                    "repair_available": 2,
                },
                "backlog_triage": {
                    "enabled": True,
                    "pending_cases": 9,
                    "interval_seconds": 60,
                    "analyses": 3,
                    "runs": 5,
                    "in_flight_patterns": 1,
                    "last_error": "",
                },
            }
        }
    )
    assert "Active backlog triage" in card
    assert "Triage worker" in card and "ON" in card
    assert "Pending" in card and ">9<" in card
    assert "Diagnosed" in card and ">3<" in card
    assert "Repair ready" in card and ">2<" in card
    assert "60s" in card
    assert "Automatic repairs" not in card
