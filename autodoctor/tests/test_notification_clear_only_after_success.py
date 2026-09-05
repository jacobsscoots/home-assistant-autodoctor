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
        self.fail = True

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        _ = title, message, notification_id

    async def dismiss_notification(self, notification_id: str) -> None:
        _ = notification_id
        if self.fail:
            raise RuntimeError("temporary failure")


def test_notification_marker_is_not_cleared_until_home_assistant_accepts_dismissal(tmp_path: Path) -> None:
    async def run() -> None:
        ha = HA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        event = LogEvent("ERROR", "demo", "demo.py:1", "failure", "", 1.0)
        pattern = "demo/x"
        await manager.record_event(
            pattern_key=pattern,
            pattern_label="demo",
            family="demo",
            fingerprint="fp",
            event=event,
            fingerprint_is_new=True,
        )
        await manager.publish_case(pattern, force=True)
        await manager.mark_resolved(pattern)
        failed = await manager.get_case(pattern)
        assert failed is not None and failed["last_notification_at"] is not None
        ha.fail = False
        assert await manager.reconcile_inactive_notifications() == 1
        cleaned = await manager.get_case(pattern)
        assert cleaned is not None and cleaned["last_notification_at"] is None

    asyncio.run(run())
