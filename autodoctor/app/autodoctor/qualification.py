from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from typing import Any


class QualificationReader:
    """Return repair-qualification counters without mutating AutoDoctor state."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def summary(self, since: float | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._summary_sync, since)

    def _summary_sync(self, since: float | None) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp()
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as db:
            case_rows = db.execute(
                "SELECT status, COUNT(*) FROM incident_cases GROUP BY status"
            ).fetchall()
            cases_by_status = {str(status): int(count) for status, count in case_rows}
            repair_plans = int(db.execute("SELECT COUNT(*) FROM repair_plans").fetchone()[0])
            repair_executions = int(
                db.execute("SELECT COUNT(*) FROM repair_executions").fetchone()[0]
            )
            successful_real_repairs = int(
                db.execute(
                    "SELECT COUNT(*) FROM repair_executions WHERE status = 'succeeded'"
                ).fetchone()[0]
            )
            ai_usage_stuck = int(
                db.execute(
                    "SELECT COUNT(*) FROM ai_usage WHERE status IN ('reserved','inflight','pending')"
                ).fetchone()[0]
            )
            month_spend = float(
                db.execute(
                    "SELECT COALESCE(SUM(cost_usd),0) FROM ai_usage WHERE ts >= ?",
                    (month_start,),
                ).fetchone()[0]
            )
            since_spend = None
            if since is not None:
                since_spend = float(
                    db.execute(
                        "SELECT COALESCE(SUM(cost_usd),0) FROM ai_usage WHERE ts >= ?",
                        (float(since),),
                    ).fetchone()[0]
                )

        return {
            "generated_at": now.timestamp(),
            "since": since,
            "cases_total": sum(cases_by_status.values()),
            "cases_by_status": cases_by_status,
            "investigating_cases": int(cases_by_status.get("investigating", 0)),
            "repair_available_cases": int(cases_by_status.get("repair_available", 0)),
            "repair_plans": repair_plans,
            "repair_executions": repair_executions,
            "successful_real_repairs": successful_real_repairs,
            "ai_usage_stuck": ai_usage_stuck,
            "ai_spend_month_usd": month_spend,
            "ai_spend_since_usd": since_spend,
        }
