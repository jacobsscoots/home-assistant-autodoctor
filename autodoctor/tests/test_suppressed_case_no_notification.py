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
        self.notifications = 0

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        _ = title, message, notification_id
        self.notifications += 1

    async def dismiss_notification(self, notification_id: str) -> None:
        _ = notification_id


def test_suppressed_case_cannot_publish_even_when_forced(tmp_path: Path) -> None:
    async def run() -> None:
        ha = HA()
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), ha)
        await manager.initialize()
        pattern = "kasa/authentication/x"
        event = LogEvent(
            "ERROR",
            "kasa.protocol",
            "['components/tplink/coordinator.py', 78]",
            "sub-call failure",
            "",
            1.0,
        )
        await manager.record_event(
            pattern_key=pattern,
            pattern_label="authentication",
            family="kasa",
            fingerprint="fp",
            event=event,
            fingerprint_is_new=True,
        )
        assert await manager.mark_suppressed_nonfatal(pattern, "observational")
        assert await manager.publish_case(pattern, force=True) is False
        assert ha.notifications == 0

    asyncio.run(run())
