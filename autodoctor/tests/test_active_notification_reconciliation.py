from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager
from autodoctor.models import LogEvent


class HA:
    def __init__(self) -> None:
        self.notifications: list[str] = []

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        _ = title, message
        self.notifications.append(notification_id)

    async def dismiss_notification(self, notification_id: str) -> None:
        _ = notification_id


def test_startup_active_publish_only_targets_active_statuses(tmp_path: Path) -> None:
    async def run() -> None:
        ha = HA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        event = LogEvent("ERROR", "demo", "demo.py:1", "failed", "", 1.0)
        for index, status in enumerate(("new", "diagnosed", "historical", "resolved", "suppressed_nonfatal")):
            pattern = f"demo/{index}"
            await manager.record_event(
                pattern_key=pattern,
                pattern_label=pattern,
                family="demo",
                fingerprint=f"fp{index}",
                event=event,
                fingerprint_is_new=True,
            )
            await manager._set_status(pattern, status)
        published = await manager.publish_active_cases(force=True)
        assert published == 2
        assert len(ha.notifications) == 2

    asyncio.run(run())
