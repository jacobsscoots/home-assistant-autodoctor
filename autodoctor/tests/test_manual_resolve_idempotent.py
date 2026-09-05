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
        self.dismissed: list[str] = []

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        _ = title, message, notification_id

    async def dismiss_notification(self, notification_id: str) -> None:
        self.dismissed.append(notification_id)


def test_mark_resolved_is_idempotent_after_notification_cleanup(tmp_path: Path) -> None:
    async def run() -> None:
        ha = HA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        pattern = "demo/x"
        event = LogEvent("ERROR", "demo", "demo.py:1", "failed", "", 1.0)
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
        await manager.mark_resolved(pattern)
        case = await manager.get_case(pattern)
        assert case is not None and case["status"] == "resolved"
        assert len(ha.dismissed) == 1

    asyncio.run(run())
