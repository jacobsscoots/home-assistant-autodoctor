from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

_ACTIVE_TRIAGE_STATUSES = ("new", "reopened")


def _now() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


async def retire_orphaned_active_case(db_path: str, pattern_key: str) -> bool:
    """Retire an active case only when no retained incident still maps to its pattern.

    The existence check and lifecycle update run under one SQLite write transaction so
    a concurrent live incident cannot be hidden: either the incident is visible and the
    retirement is refused, or it arrives after commit and normal case ingestion reopens
    the historical case.
    """

    return await asyncio.to_thread(_retire_orphaned_active_case_sync, db_path, pattern_key)


def _retire_orphaned_active_case_sync(db_path: str, pattern_key: str) -> bool:
    key = str(pattern_key or "")
    if not key:
        return False

    with sqlite3.connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        retained = db.execute(
            "SELECT 1 FROM incidents WHERE pattern_key = ? LIMIT 1",
            (key,),
        ).fetchone()
        if retained is not None:
            db.commit()
            return False

        placeholders = ",".join("?" for _ in _ACTIVE_TRIAGE_STATUSES)
        cursor = db.execute(
            f"""UPDATE incident_cases
                SET status = 'historical', updated_at = ?
                WHERE pattern_key = ? AND status IN ({placeholders})""",
            (_now(), key, *_ACTIVE_TRIAGE_STATUSES),
        )
        db.commit()
        return cursor.rowcount == 1
