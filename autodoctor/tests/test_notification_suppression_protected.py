from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager
from autodoctor.models import LogEvent


class HA:
    async def dismiss_notification(self, notification_id: str) -> None:
        _ = notification_id


def test_suppression_protects_every_user_or_execution_sensitive_status(tmp_path: Path) -> None:
    async def run() -> None:
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), HA())
        await manager.initialize()
        event = LogEvent("ERROR", "kasa.protocol", "components/tplink/coordinator.py:78", "failure", "", 1.0)
        for index, status in enumerate(("investigating", "repair_available", "needs_user_action", "verifying")):
            pattern = f"kasa/{index}"
            await manager.record_event(
                pattern_key=pattern,
                pattern_label="noise",
                family="kasa",
                fingerprint=f"fp{index}",
                event=event,
                fingerprint_is_new=True,
            )
            await manager._set_status(pattern, status)
            assert not await manager.mark_suppressed_nonfatal(pattern, "noise")
            case = await manager.get_case(pattern)
            assert case is not None and case["status"] == status

    asyncio.run(run())
