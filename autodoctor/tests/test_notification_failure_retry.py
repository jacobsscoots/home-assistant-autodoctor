from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager
from autodoctor.models import LogEvent


class FlakyHA:
    def __init__(self) -> None:
        self.notifications: list[str] = []
        self.dismissed: list[str] = []
        self.fail_next_dismiss = True

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        _ = title, message
        self.notifications.append(notification_id)

    async def dismiss_notification(self, notification_id: str) -> None:
        if self.fail_next_dismiss:
            self.fail_next_dismiss = False
            raise RuntimeError("temporary Home Assistant failure")
        self.dismissed.append(notification_id)


def test_inactive_notification_dismissal_is_retried_after_transient_failure(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FlakyHA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        pattern = "demo/other/x"
        event = LogEvent(
            level="ERROR",
            name="homeassistant.components.demo",
            source="components/demo/coordinator.py:1",
            message="demo failure",
            exception="",
            timestamp=1000.0,
        )
        await manager.record_event(
            pattern_key=pattern,
            pattern_label="demo",
            family="homeassistant.components.demo",
            fingerprint="fp",
            event=event,
            fingerprint_is_new=True,
        )
        assert await manager.publish_case(pattern, force=True)

        await manager.mark_resolved(pattern)
        case = await manager.get_case(pattern)
        assert case is not None
        assert case["status"] == "resolved"
        # The marker deliberately remains when HA dismissal fails, so periodic/startup
        # reconciliation has a durable indication that cleanup is still needed.
        assert case["last_notification_at"] is not None
        assert manager.notification_dismiss_failures == 1

        assert await manager.reconcile_inactive_notifications() == 1
        case = await manager.get_case(pattern)
        assert case is not None
        assert case["last_notification_at"] is None
        assert manager.notification_dismissals == 1
        assert len(ha.dismissed) == 1

    asyncio.run(run())
