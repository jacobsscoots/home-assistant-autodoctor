from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .budget import month_bounds_utc
from .models import Analysis, LogEvent
from .scheduler import incident_family

_INCIDENT_SCHEMA = """
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
"""

_AI_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    family TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reserved_input_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reserved_cost_usd REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage(ts);
CREATE INDEX IF NOT EXISTS idx_ai_usage_family_ts ON ai_usage(family, ts);
"""

_V012_AI_USAGE_COLUMNS = {
    "id",
    "ts",
    "fingerprint",
    "provider",
    "model",
    "status",
    "reserved_input_tokens",
    "reserved_output_tokens",
    "input_tokens",
    "output_tokens",
    "reserved_cost_usd",
    "cost_usd",
    "error",
}
_REQUIRED_AI_USAGE_COLUMNS = _V012_AI_USAGE_COLUMNS | {"family"}
_LEGACY_AI_USAGE_COLUMNS = {"ts", "fingerprint"}


class IncidentStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.executescript(_INCIDENT_SCHEMA)
            self._ensure_ai_usage_schema(db)

    @staticmethod
    def _backfill_ai_usage_families(db: sqlite3.Connection) -> None:
        rows = db.execute(
            """SELECT u.id, i.name, i.source
            FROM ai_usage AS u
            LEFT JOIN incidents AS i ON i.fingerprint = u.fingerprint
            WHERE COALESCE(u.family, '') = ''"""
        ).fetchall()
        updates: list[tuple[str, int]] = []
        for usage_id, name, source in rows:
            if name is None and source is None:
                continue
            family = incident_family(str(name or ""), str(source or ""))
            if family != "unknown":
                updates.append((family, int(usage_id)))
        if updates:
            db.executemany("UPDATE ai_usage SET family = ? WHERE id = ?", updates)

    @staticmethod
    def _ensure_ai_usage_schema(db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(ai_usage)").fetchall()}
        if not columns:
            db.executescript(_AI_USAGE_SCHEMA)
            return

        if _REQUIRED_AI_USAGE_COLUMNS.issubset(columns):
            db.executescript(_AI_USAGE_SCHEMA)
            IncidentStore._backfill_ai_usage_families(db)
            db.commit()
            return

        # v0.1.2 already has the full budget ledger. Add only the new family
        # column in-place so successful/failed reservations and token history
        # survive the v0.1.3 scheduler upgrade.
        if _V012_AI_USAGE_COLUMNS.issubset(columns):
            db.execute("ALTER TABLE ai_usage ADD COLUMN family TEXT NOT NULL DEFAULT ''")
            db.executescript(_AI_USAGE_SCHEMA)
            IncidentStore._backfill_ai_usage_families(db)
            db.commit()
            return

        # v0.1.0/v0.1.1 stored only (ts, fingerprint). Preserve those rows as
        # zero-cost historical successes. Refuse any unknown schema rather than
        # guessing and risking destructive migration.
        if columns == _LEGACY_AI_USAGE_COLUMNS:
            db.execute("DROP INDEX IF EXISTS idx_ai_usage_ts")
            db.execute("DROP TABLE IF EXISTS ai_usage_legacy_v011")
            db.execute("ALTER TABLE ai_usage RENAME TO ai_usage_legacy_v011")
            db.executescript(_AI_USAGE_SCHEMA)
            db.execute(
                """INSERT INTO ai_usage
                (ts, fingerprint, provider, model, family, status,
                 reserved_input_tokens, reserved_output_tokens,
                 input_tokens, output_tokens, reserved_cost_usd, cost_usd, error)
                SELECT ts, fingerprint, '', '', '', 'legacy_success',
                       0, 0, NULL, NULL, 0, 0, NULL
                FROM ai_usage_legacy_v011"""
            )
            IncidentStore._backfill_ai_usage_families(db)
            db.execute("DROP TABLE ai_usage_legacy_v011")
            db.commit()
            return

        raise RuntimeError(
            "Unsupported ai_usage schema; refusing destructive migration. "
            f"Columns: {sorted(columns)}"
        )

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
            db.commit()

    async def mark_analysis_attempt(self, fp: str, now_ts: float | None = None) -> None:
        now = now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp()
        async with self._lock:
            await asyncio.to_thread(self._mark_analysis_attempt_sync, fp, now)

    def _mark_analysis_attempt_sync(self, fp: str, now: float) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE incidents SET last_analysis_at = ? WHERE fingerprint = ?",
                (float(now), fp),
            )
            db.commit()

    async def reserve_ai_usage(
        self,
        *,
        fingerprint: str,
        provider: str,
        model: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_usd: float,
        monthly_stop_usd: float,
        now_ts: float | None = None,
        family: str = "",
    ) -> tuple[int | None, float]:
        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_ai_usage_sync,
                fingerprint,
                provider,
                model,
                reserved_input_tokens,
                reserved_output_tokens,
                reserved_cost_usd,
                monthly_stop_usd,
                now_ts,
                family,
            )

    def _reserve_ai_usage_sync(
        self,
        fingerprint: str,
        provider: str,
        model: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_usd: float,
        monthly_stop_usd: float,
        now_ts: float | None,
        family: str,
    ) -> tuple[int | None, float]:
        now = now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp()
        month_start, month_end, _ = month_bounds_utc(now)
        safe_family = str(family or "unknown")[:200]
        with sqlite3.connect(self.path) as db:
            spent = float(
                db.execute(
                    """SELECT COALESCE(SUM(cost_usd), 0)
                    FROM ai_usage
                    WHERE ts >= ? AND ts < ? AND status != 'blocked_budget'""",
                    (month_start, month_end),
                ).fetchone()[0]
            )
            if spent + reserved_cost_usd > monthly_stop_usd + 1e-12:
                db.execute(
                    """INSERT INTO ai_usage
                    (ts, fingerprint, provider, model, family, status,
                     reserved_input_tokens, reserved_output_tokens,
                     reserved_cost_usd, cost_usd, error)
                    VALUES (?, ?, ?, ?, ?, 'blocked_budget', ?, ?, ?, 0, ?)""",
                    (
                        now,
                        fingerprint,
                        provider,
                        model,
                        safe_family,
                        max(0, int(reserved_input_tokens)),
                        max(0, int(reserved_output_tokens)),
                        max(0.0, float(reserved_cost_usd)),
                        f"monthly stop ${monthly_stop_usd:.8f} would be exceeded",
                    ),
                )
                db.commit()
                return None, spent

            cursor = db.execute(
                """INSERT INTO ai_usage
                (ts, fingerprint, provider, model, family, status,
                 reserved_input_tokens, reserved_output_tokens,
                 reserved_cost_usd, cost_usd)
                VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)""",
                (
                    now,
                    fingerprint,
                    provider,
                    model,
                    safe_family,
                    max(0, int(reserved_input_tokens)),
                    max(0, int(reserved_output_tokens)),
                    max(0.0, float(reserved_cost_usd)),
                    max(0.0, float(reserved_cost_usd)),
                ),
            )
            db.commit()
            return int(cursor.lastrowid), spent

    async def finalize_ai_usage(
        self,
        usage_id: int,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._finalize_ai_usage_sync,
                usage_id,
                input_tokens,
                output_tokens,
                cost_usd,
            )

    def _finalize_ai_usage_sync(
        self,
        usage_id: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """UPDATE ai_usage
                SET status = 'succeeded', input_tokens = ?, output_tokens = ?,
                    cost_usd = ?, error = NULL
                WHERE id = ?""",
                (
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0.0, float(cost_usd)),
                    int(usage_id),
                ),
            )
            db.commit()

    async def fail_ai_usage(self, usage_id: int, error: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._fail_ai_usage_sync, usage_id, error)

    def _fail_ai_usage_sync(self, usage_id: int, error: str) -> None:
        with sqlite3.connect(self.path) as db:
            # Keep cost_usd at the pre-call reservation when actual usage is
            # unavailable. This prevents failed/aborted calls from bypassing budget.
            db.execute(
                "UPDATE ai_usage SET status = 'failed', error = ? WHERE id = ?",
                (str(error)[:1000], int(usage_id)),
            )
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
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM ai_usage WHERE ts >= ?",
                    (since_ts,),
                ).fetchone()[0]
            )

    async def ai_count_for_family_since(self, family: str, since_ts: float) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._ai_count_for_family_since_sync, family, since_ts)

    def _ai_count_for_family_since_sync(self, family: str, since_ts: float) -> int:
        with sqlite3.connect(self.path) as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM ai_usage WHERE family = ? AND ts >= ?",
                    (str(family), float(since_ts)),
                ).fetchone()[0]
            )

    async def ai_family_counts_since(self, since_ts: float) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._ai_family_counts_since_sync, since_ts)

    def _ai_family_counts_since_sync(self, since_ts: float) -> dict[str, int]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                """SELECT family, COUNT(*)
                FROM ai_usage
                WHERE ts >= ? AND family != ''
                GROUP BY family
                ORDER BY COUNT(*) DESC, family ASC
                LIMIT 20""",
                (float(since_ts),),
            ).fetchall()
        return {str(family): int(count) for family, count in rows}

    async def monthly_ai_usage(self, now_ts: float | None = None) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._monthly_ai_usage_sync, now_ts)

    def _monthly_ai_usage_sync(self, now_ts: float | None) -> dict[str, Any]:
        now = now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp()
        month_start, month_end, month = month_bounds_utc(now)
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                """SELECT
                    COALESCE(SUM(CASE WHEN status != 'blocked_budget' THEN cost_usd ELSE 0 END), 0),
                    SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status IN ('reserved','succeeded','failed','legacy_success') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'blocked_budget' THEN 1 ELSE 0 END),
                    COALESCE(SUM(input_tokens), 0),
                    COALESCE(SUM(output_tokens), 0)
                FROM ai_usage
                WHERE ts >= ? AND ts < ?""",
                (month_start, month_end),
            ).fetchone()
        return {
            "month_utc": month,
            "spent_usd": float(row[0] or 0),
            "analyses_count": int(row[1] or 0),
            "attempts_count": int(row[2] or 0),
            "failed_count": int(row[3] or 0),
            "reserved_count": int(row[4] or 0),
            "budget_blocked_count": int(row[5] or 0),
            "input_tokens": int(row[6] or 0),
            "output_tokens": int(row[7] or 0),
        }
