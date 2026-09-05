from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager


class HA:
    async def dismiss_notification(self, notification_id: str) -> None:
        raise AssertionError(f"should not dismiss when notifications are disabled: {notification_id}")


def test_notification_lifecycle_is_noop_when_notifications_are_disabled(tmp_path: Path) -> None:
    async def run() -> None:
        manager = LifecycleIncidentCaseManager(
            str(tmp_path / "db.sqlite"),
            HA(),
            notifications_enabled=False,
        )
        await manager.initialize()
        assert await manager.reconcile_inactive_notifications() == 0
        assert await manager.publish_active_cases(force=True) == 0

    asyncio.run(run())
