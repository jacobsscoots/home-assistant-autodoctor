from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_lifecycle import LifecycleIncidentCaseManager


class HA:
    async def dismiss_notification(self, notification_id: str) -> None:
        _ = notification_id


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
