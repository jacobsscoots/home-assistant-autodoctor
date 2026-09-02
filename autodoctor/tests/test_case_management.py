from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.cases import IncidentCaseManager
from autodoctor.models import Analysis, LogEvent


class FakeHA:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str, str]] = []
        self.dismissed: list[str] = []

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        self.notifications.append((title, message, notification_id))

    async def dismiss_notification(self, notification_id: str) -> None:
        self.dismissed.append(notification_id)


def event(ts: float = 1000.0) -> LogEvent:
    return LogEvent(
        level="ERROR",
        source="components/kasa",
        exception="",
        message="Authentication failed for switch.office",
        name="kasa.auth",
        timestamp=ts,
    )


def test_same_pattern_owns_one_case_and_notification(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = IncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        first, new_first = await manager.record_event(
            pattern_key="kasa/authentication/abc",
            pattern_label="authentication",
            family="kasa",
            fingerprint="fp1",
            event=event(1000),
            fingerprint_is_new=True,
        )
        second, new_second = await manager.record_event(
            pattern_key="kasa/authentication/abc",
            pattern_label="authentication",
            family="kasa",
            fingerprint="fp2",
            event=event(1001),
            fingerprint_is_new=True,
        )
        assert new_first is True
        assert new_second is False
        assert second["occurrences"] == 2
        assert second["fingerprint_count"] == 2
        assert first["notification_id"] == second["notification_id"]
        assert await manager.publish_case("kasa/authentication/abc", force=True)
        assert len(ha.notifications) == 1
        assert ha.notifications[0][2].startswith("autodoctor_case_")

    asyncio.run(run())


def test_backlog_reconciliation_retires_legacy_and_keeps_stale_historical(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = IncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        now = manager._now()
        result = await manager.reconcile_backlog(
            [
                {
                    "fingerprint": "oldfp",
                    "pattern_key": "old/pattern/1",
                    "pattern_label": "old failure",
                    "first_seen": now - 200000,
                    "last_seen": now - 172800,
                    "occurrences": 20,
                }
            ]
        )
        assert result["cases"] == 1
        assert ha.dismissed == ["autodoctor_oldfp"]
        case = await manager.get_case("old/pattern/1")
        assert case is not None
        assert case["status"] == "historical"
        assert ha.notifications == []

    asyncio.run(run())


def test_recent_backlog_collapses_multiple_fingerprints_to_one_case_notice(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = IncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        now = manager._now()
        await manager.reconcile_backlog(
            [
                {
                    "fingerprint": "fp1",
                    "pattern_key": "pattern/recent/x",
                    "pattern_label": "recent failure",
                    "first_seen": now - 100,
                    "last_seen": now - 10,
                    "occurrences": 5,
                },
                {
                    "fingerprint": "fp2",
                    "pattern_key": "pattern/recent/x",
                    "pattern_label": "recent failure",
                    "first_seen": now - 80,
                    "last_seen": now - 5,
                    "occurrences": 7,
                },
            ]
        )
        case = await manager.get_case("pattern/recent/x")
        assert case is not None
        assert case["occurrences"] == 12
        assert case["fingerprint_count"] == 2
        assert len(ha.notifications) == 1
        assert set(ha.dismissed) == {"autodoctor_fp1", "autodoctor_fp2"}

    asyncio.run(run())


def test_analysis_creates_non_executable_repair_plan(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = IncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        await manager.record_event(
            pattern_key="automation/failure/x",
            pattern_label="automation failure",
            family="automation",
            fingerprint="fp",
            event=event(),
            fingerprint_is_new=True,
        )
        analysis = Analysis(
            summary="Reload the affected config entry after confirming it is the failing integration.",
            root_cause="Config entry became unhealthy.",
            confidence=0.92,
            risk="low",
            action="propose_fix",
            checks=["Confirm exact config entry"],
            proposed_changes=[
                {
                    "operation": "reload_config_entry",
                    "target": "entry_abc",
                    "reason": "recover unhealthy integration",
                }
            ],
        )
        plan = await manager.apply_analysis(
            pattern_key="automation/failure/x",
            fingerprint="fp",
            analysis=analysis,
            evidence={"source": "test"},
        )
        assert plan is not None
        assert plan["repair_type"] == "reload_config_entry"
        stored = await manager.list_repair_plans()
        assert len(stored) == 1
        assert stored[0]["status"] == "proposed"
        health = await manager.health()
        assert health["executor_enabled"] is False
        case = await manager.get_case("automation/failure/x")
        assert case is not None
        assert case["status"] == "repair_available"

    asyncio.run(run())
