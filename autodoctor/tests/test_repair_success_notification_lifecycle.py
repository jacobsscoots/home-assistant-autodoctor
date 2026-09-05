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
        self.notifications: list[str] = []
        self.dismissed: list[str] = []

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        _ = title, message
        self.notifications.append(notification_id)

    async def dismiss_notification(self, notification_id: str) -> None:
        self.dismissed.append(notification_id)


def test_verified_fix_resolution_dismisses_case_notification(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        pattern = "demo/not_found/x"
        event = LogEvent(
            level="ERROR",
            name="homeassistant.components.demo.coordinator",
            source="helpers/update_coordinator.py:506",
            message="Update failed",
            exception="",
            timestamp=1000.0,
        )
        await manager.record_event(
            pattern_key=pattern,
            pattern_label="not_found",
            family="homeassistant.components.demo",
            fingerprint="fp",
            event=event,
            fingerprint_is_new=True,
        )
        await manager._set_status(pattern, "verifying")
        assert await manager.publish_case(pattern, force=True)
        owned = ha.notifications[-1]

        # RepairExecutor calls this exact case-manager method after verification passes.
        await manager.mark_resolved(pattern, verification="verified reload")
        case = await manager.get_case(pattern)
        assert case is not None
        assert case["status"] == "resolved"
        assert case["last_notification_at"] is None
        assert ha.dismissed == [owned]

    asyncio.run(run())
