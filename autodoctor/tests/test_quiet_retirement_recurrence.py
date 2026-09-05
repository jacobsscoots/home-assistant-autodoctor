from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager
from autodoctor.models import LogEvent


class HA:
    async def notify(self, title: str, message: str, notification_id: str) -> None:
        _ = title, message, notification_id

    async def dismiss_notification(self, notification_id: str) -> None:
        _ = notification_id


def test_historical_quiet_case_reopens_on_recurrence(tmp_path: Path) -> None:
    async def run() -> None:
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), HA())
        await manager.initialize()
        pattern = "demo/x"
        first = LogEvent("ERROR", "demo", "demo.py:1", "failed", "", 1000.0)
        await manager.record_event(
            pattern_key=pattern,
            pattern_label="demo",
            family="demo",
            fingerprint="fp",
            event=first,
            fingerprint_is_new=True,
        )
        assert await manager.retire_quiet_cases(quiet_seconds=3600, now=5001.0) == 1
        recurrence = LogEvent("ERROR", "demo", "demo.py:1", "failed", "", 6000.0)
        await manager.record_event(
            pattern_key=pattern,
            pattern_label="demo",
            family="demo",
            fingerprint="fp",
            event=recurrence,
            fingerprint_is_new=False,
        )
        case = await manager.get_case(pattern)
        assert case is not None and case["status"] == "reopened"

    asyncio.run(run())
