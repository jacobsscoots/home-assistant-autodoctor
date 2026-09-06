from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.qualification import QualificationReader


SCHEMA = """
CREATE TABLE incident_cases (status TEXT NOT NULL);
CREATE TABLE repair_plans (plan_id TEXT PRIMARY KEY);
CREATE TABLE repair_executions (execution_id TEXT PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE ai_usage (ts REAL NOT NULL, status TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0);
"""


def test_qualification_summary_is_authoritative_and_read_only(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "autodoctor.db"
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA)
            db.executemany(
                "INSERT INTO incident_cases(status) VALUES (?)",
                [("new",), ("investigating",), ("repair_available",)],
            )
            db.executemany("INSERT INTO repair_plans(plan_id) VALUES (?)", [("p1",), ("p2",)])
            db.executemany(
                "INSERT INTO repair_executions(execution_id,status) VALUES (?,?)",
                [("e1", "succeeded"), ("e2", "failed")],
            )
            db.executemany(
                "INSERT INTO ai_usage(ts,status,cost_usd) VALUES (?,?,?)",
                [(1000, "reserved", 0.01), (2000, "completed", 0.02)],
            )
            db.commit()

        summary = await QualificationReader(str(path)).summary(since=0)
        assert summary["cases_total"] == 3
        assert summary["investigating_cases"] == 1
        assert summary["repair_available_cases"] == 1
        assert summary["repair_plans"] == 2
        assert summary["repair_executions"] == 2
        assert summary["successful_real_repairs"] == 1
        assert summary["ai_usage_stuck"] == 1
        assert summary["ai_spend_since_usd"] == 0.03

        with sqlite3.connect(path) as db:
            assert db.execute("SELECT COUNT(*) FROM incident_cases").fetchone()[0] == 3
            assert db.execute("SELECT COUNT(*) FROM repair_executions").fetchone()[0] == 2

    asyncio.run(run())
