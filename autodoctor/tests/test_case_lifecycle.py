from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager
from autodoctor.models import LogEvent


class FakeHA:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str, str]] = []
        self.dismissed: list[str] = []

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        self.notifications.append((title, message, notification_id))

    async def dismiss_notification(self, notification_id: str) -> None:
        self.dismissed.append(notification_id)


def _event(ts: float) -> LogEvent:
    return LogEvent(
        level="ERROR",
        name="kasa.protocol",
        source="['components/tplink/coordinator.py', 78]",
        message="sub-call failed",
        exception="",
        timestamp=ts,
    )


async def _record(manager: LifecycleIncidentCaseManager, pattern: str, ts: float) -> dict:
    case, _ = await manager.record_event(
        pattern_key=pattern,
        pattern_label="authentication",
        family="kasa",
        fingerprint="fp",
        event=_event(ts),
        fingerprint_is_new=True,
    )
    return case


def test_suppressed_nonfatal_case_dismisses_owned_notification(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        await _record(manager, "kasa/authentication/x", 1000.0)
        assert await manager.publish_case("kasa/authentication/x", force=True)
        assert len(ha.notifications) == 1

        changed = await manager.mark_suppressed_nonfatal(
            "kasa/authentication/x",
            "retained as correlation evidence only",
        )
        assert changed is True
        case = await manager.get_case("kasa/authentication/x")
        assert case is not None
        assert case["status"] == "suppressed_nonfatal"
        assert case["last_notification_at"] is None
        assert ha.dismissed == [case["notification_id"]]
        assert not await manager.publish_case("kasa/authentication/x", force=True)

    asyncio.run(run())


def test_suppression_never_hides_repair_available_case(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        await _record(manager, "kasa/authentication/x", 1000.0)
        await manager._set_status("kasa/authentication/x", "repair_available")
        assert not await manager.mark_suppressed_nonfatal("kasa/authentication/x", "noise")
        case = await manager.get_case("kasa/authentication/x")
        assert case is not None
        assert case["status"] == "repair_available"

    asyncio.run(run())


def test_nonmatching_future_event_can_reopen_suppressed_case(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        await _record(manager, "kasa/authentication/x", 1000.0)
        assert await manager.mark_suppressed_nonfatal("kasa/authentication/x", "noise")
        assert await manager.reopen_if_suppressed("kasa/authentication/x")
        case = await manager.get_case("kasa/authentication/x")
        assert case is not None
        assert case["status"] == "reopened"
        assert case["confidence"] == 0

    asyncio.run(run())


def test_quiet_diagnosed_case_becomes_historical_and_notice_is_dismissed(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        await _record(manager, "pattern/quiet/x", 1000.0)
        await manager._set_status("pattern/quiet/x", "diagnosed")
        assert await manager.publish_case("pattern/quiet/x", force=True)

        retired = await manager.retire_quiet_cases(quiet_seconds=3600, now=5001.0)
        assert retired == 1
        case = await manager.get_case("pattern/quiet/x")
        assert case is not None
        assert case["status"] == "historical"
        assert case["last_notification_at"] is None
        assert ha.dismissed == [case["notification_id"]]

    asyncio.run(run())


def test_quiet_user_action_case_is_never_auto_retired(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        await _record(manager, "pattern/action/x", 1000.0)
        await manager._set_status("pattern/action/x", "needs_user_action")
        assert await manager.retire_quiet_cases(quiet_seconds=3600, now=5001.0) == 0
        case = await manager.get_case("pattern/action/x")
        assert case is not None
        assert case["status"] == "needs_user_action"

    asyncio.run(run())


def test_manual_resolution_clears_notification_marker_and_recurrence_reopens(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        await _record(manager, "pattern/manual/x", 1000.0)
        assert await manager.publish_case("pattern/manual/x", force=True)
        await manager.mark_resolved("pattern/manual/x", verification="manual dashboard resolution")
        resolved = await manager.get_case("pattern/manual/x")
        assert resolved is not None
        assert resolved["status"] == "resolved"
        assert resolved["last_notification_at"] is None

        await manager.record_event(
            pattern_key="pattern/manual/x",
            pattern_label="authentication",
            family="kasa",
            fingerprint="fp",
            event=_event(2000.0),
            fingerprint_is_new=False,
        )
        reopened = await manager.get_case("pattern/manual/x")
        assert reopened is not None
        assert reopened["status"] == "reopened"

    asyncio.run(run())
