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


def test_only_first_inactive_reconciliation_is_implicit_force(tmp_path: Path) -> None:
    async def run() -> None:
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), HA())
        await manager.initialize()
        assert manager._inactive_notification_reconciliations == 0
        await manager.reconcile_inactive_notifications()
        assert manager._inactive_notification_reconciliations == 1
        await manager.reconcile_inactive_notifications()
        assert manager._inactive_notification_reconciliations == 2

    asyncio.run(run())


def test_inactive_notification_reconciliation_is_not_limited_to_500_cases(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "db.sqlite")
        ha = HA()
        manager = LifecycleIncidentCaseManager(db_path, ha)
        await manager.initialize()
        now = manager._now()

        with sqlite3.connect(db_path) as db:
            for index in range(501):
                pattern_key = f"resolved/{index}"
                db.execute(
                    """INSERT INTO incident_cases
                    (pattern_key, pattern_label, family, status, first_seen, last_seen,
                     occurrences, fingerprint_count, representative_fingerprint,
                     notification_id, last_notification_at, updated_at)
                    VALUES (?, ?, ?, 'resolved', ?, ?, 1, 1, ?, ?, ?, ?)""",
                    (
                        pattern_key,
                        "resolved",
                        "demo",
                        now - index,
                        now - index,
                        f"fp-{index}",
                        manager.notification_id(pattern_key),
                        now,
                        now,
                    ),
                )
            db.commit()

        dismissed = await manager.reconcile_inactive_notifications()
        assert dismissed == 501
        assert len(ha.dismissed) == 501

    asyncio.run(run())
