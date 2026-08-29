from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .models import Analysis, LogEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    fingerprint TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    occurrences INTEGER NOT NULL,
    level TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    exception TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    last_analysis_at REAL,
    analysis_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_last_seen ON incidents(last_seen DESC);
CREATE TABLE IF NOT EXISTS ai_usage (
    ts REAL NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage(ts);
"""


class IncidentStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)

    async def record(self, fp: str, event: LogEvent) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            return await asyncio.to_thread(self._record_sync, fp, event)

    def _record_sync(self, fp: str, event: LogEvent) -> tuple[dict[str, Any], bool]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            existing = db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (fp,)).fetchone()
            is_new = existing is None
            if is_new:
                db.execute(
                    """INSERT INTO incidents
                    (fingerprint, first_seen, last_seen, occurrences, level, name, source, message, exception, status)
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 'open')""",
                    (fp, event.timestamp, event.timestamp, event.level, event.name, event.source, event.message, event.exception),
                )
            else:
                db.execute(
                    """UPDATE incidents
                    SET last_seen = ?, occurrences = occurrences + 1, level = ?, name = ?, source = ?,
                        message = ?, exception = ?, status = CASE WHEN status = 'resolved' THEN 'reopened' ELSE status END
                    WHERE fingerprint = ?""",
                    (event.timestamp, event.level, event.name, event.source, event.message, event.exception, fp),
                )
            db.commit()
            row = db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (fp,)).fetchone()
            return dict(row), is_new

    async def save_analysis(self, fp: str, analysis: Analysis) -> None:
        payload = json.dumps(analysis.raw or analysis.__dict__, separators=(",", ":"))
        now = datetime.now(tz=timezone.utc).timestamp()
        async with self._lock:
            await asyncio.to_thread(self._save_analysis_sync, fp, payload, now)

    def _save_analysis_sync(self, fp: str, payload: str, now: float) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE incidents SET analysis_json = ?, last_analysis_at = ? WHERE fingerprint = ?",
                (payload, now, fp),
            )
            db.execute("INSERT INTO ai_usage(ts, fingerprint) VALUES (?, ?)", (now, fp))
            db.commit()

    async def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_recent_sync, limit)

    def _list_recent_sync(self, limit: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM incidents ORDER BY last_seen DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    async def ai_count_since(self, since_ts: float) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._ai_count_since_sync, since_ts)

    def _ai_count_since_sync(self, since_ts: float) -> int:
        with sqlite3.connect(self.path) as db:
            return int(db.execute("SELECT COUNT(*) FROM ai_usage WHERE ts >= ?", (since_ts,)).fetchone()[0])
