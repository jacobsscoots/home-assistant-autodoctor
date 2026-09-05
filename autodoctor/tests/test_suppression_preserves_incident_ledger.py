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


def test_suppression_preserves_case_occurrence_and_fingerprint_counts(tmp_path: Path) -> None:
    async def run() -> None:
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), HA())
        await manager.initialize()
        pattern = "kasa/device_query_timeout/x"
        event = LogEvent(
            "ERROR",
            "kasa.protocol",
            "components/tplink/coordinator.py:78",
            "retry",
            "",
            1.0,
        )
        await manager.record_event(
            pattern_key=pattern,
            pattern_label="device_query_timeout",
            family="kasa",
            fingerprint="fp1",
            event=event,
            fingerprint_is_new=True,
        )
        await manager.record_event(
            pattern_key=pattern,
            pattern_label="device_query_timeout",
            family="kasa",
            fingerprint="fp2",
            event=event,
            fingerprint_is_new=True,
        )
        before = await manager.get_case(pattern)
        assert before is not None
        assert await manager.mark_suppressed_nonfatal(pattern, "observed only")
        after = await manager.get_case(pattern)
        assert after is not None
        assert after["occurrences"] == before["occurrences"] == 2
        assert after["fingerprint_count"] == before["fingerprint_count"] == 2

    asyncio.run(run())
