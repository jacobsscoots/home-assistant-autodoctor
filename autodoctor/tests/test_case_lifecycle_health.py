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


def test_lifecycle_health_exposes_notification_policy(tmp_path: Path) -> None:
    async def run() -> None:
        manager = LifecycleIncidentCaseManager(str(tmp_path / "db.sqlite"), HA())
        await manager.initialize()
        health = await manager.lifecycle_health()
        assert health["notification_policy"] == "active-cases-only"
        assert "resolved" in health["inactive_statuses"]
        assert "historical" in health["inactive_statuses"]
        assert "suppressed_nonfatal" in health["inactive_statuses"]
        assert health["quiet_retire_seconds"] == 24 * 3600

    asyncio.run(run())
