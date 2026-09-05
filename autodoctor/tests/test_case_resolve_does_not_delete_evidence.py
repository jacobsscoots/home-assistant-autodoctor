from __future__ import annotations

import asyncio
import sqlite3
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


def test_mark_resolved_changes_lifecycle_without_deleting_case_record(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "db.sqlite")
        manager = LifecycleIncidentCaseManager(db_path, HA())
        await manager.initialize()
        event = LogEvent("ERROR", "demo", "demo.py:1", "failure", "", 1.0)
        await manager.record_event(
            pattern_key="demo/x",
            pattern_label="demo",
            family="demo",
            fingerprint="fp",
            event=event,
            fingerprint_is_new=True,
        )
        await manager.mark_resolved("demo/x")
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT status, occurrences, representative_fingerprint FROM incident_cases WHERE pattern_key='demo/x'"
            ).fetchone()
        assert row == ("resolved", 1, "fp")

    asyncio.run(run())
