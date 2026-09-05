from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager


class HA:
    def __init__(self) -> None:
        self.dismissed: list[str] = []

    async def dismiss_notification(self, notification_id: str) -> None:
        self.dismissed.append(notification_id)


def test_forced_startup_cleanup_still_refuses_foreign_notification_ids(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "db.sqlite")
        ha = HA()
        manager = LifecycleIncidentCaseManager(db_path, ha)
        await manager.initialize()
        now = manager._now()
        with sqlite3.connect(db_path) as db:
            db.execute(
                """INSERT INTO incident_cases
                (pattern_key, pattern_label, family, status, first_seen, last_seen,
                 occurrences, fingerprint_count, representative_fingerprint,
                 notification_id, last_notification_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                ("demo/x", "demo", "demo", "resolved", now, now, 1, 1, "fp", "foreign_notice", now),
            )
            db.commit()
        assert await manager.reconcile_inactive_notifications() == 0
        assert ha.dismissed == []

    asyncio.run(run())
