from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager
from autodoctor.models import LogEvent


class FakeHA:
    def __init__(self) -> None:
        self.notifications: list[str] = []
        self.dismissed: list[str] = []

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        _ = title, message
        self.notifications.append(notification_id)

    async def dismiss_notification(self, notification_id: str) -> None:
        self.dismissed.append(notification_id)


def _event(ts: float) -> LogEvent:
    return LogEvent(
        level="ERROR",
        name="homeassistant.components.demo",
        source="components/demo/coordinator.py:1",
        message="demo failure",
        exception="",
        timestamp=ts,
    )


def test_verified_or_manual_resolution_dismisses_once_and_future_recurrence_can_notify_again(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "db.sqlite")
        ha = FakeHA()
        manager = LifecycleIncidentCaseManager(db_path, ha)
        await manager.initialize()
        pattern = "homeassistant.components.demo/other/x"

        await manager.record_event(
            pattern_key=pattern,
            pattern_label="demo failure",
            family="homeassistant.components.demo",
            fingerprint="fp",
            event=_event(1000.0),
            fingerprint_is_new=True,
        )
        assert await manager.publish_case(pattern, force=True)
        owned_id = ha.notifications[-1]

        await manager.mark_resolved(pattern, verification="fixed")
        await manager.mark_resolved(pattern, verification="fixed again")
        assert ha.dismissed == [owned_id]

        with sqlite3.connect(db_path) as db:
            marker = db.execute(
                "SELECT last_notification_at FROM incident_cases WHERE pattern_key = ?",
                (pattern,),
            ).fetchone()[0]
        assert marker is None

        await manager.record_event(
            pattern_key=pattern,
            pattern_label="demo failure",
            family="homeassistant.components.demo",
            fingerprint="fp",
            event=_event(2000.0),
            fingerprint_is_new=False,
        )
        case = await manager.get_case(pattern)
        assert case is not None and case["status"] == "reopened"
        assert await manager.publish_case(pattern, force=True)
        assert ha.notifications[-1] == owned_id

    asyncio.run(run())
