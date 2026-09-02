from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS autodoctor_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
_LIFECYCLE_KEY = "ai_usage_lifecycle_v1"


@dataclass(frozen=True)
class AIUsageRecoveryResult:
    legacy_unknown: int = 0
    released_pre_provider: int = 0
    retained_inflight: int = 0
    released_cost_usd: float = 0.0
    retained_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "legacy_unknown": self.legacy_unknown,
            "released_pre_provider": self.released_pre_provider,
            "retained_inflight": self.retained_inflight,
            "released_cost_usd": self.released_cost_usd,
            "retained_cost_usd": self.retained_cost_usd,
        }


async def mark_ai_usage_inflight(db_path: str, usage_id: int) -> bool:
    """Atomically mark a reservation as having entered the external provider call."""
    return await asyncio.to_thread(_mark_ai_usage_inflight_sync, db_path, usage_id)


def _mark_ai_usage_inflight_sync(db_path: str, usage_id: int) -> bool:
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "UPDATE ai_usage SET status = 'inflight' WHERE id = ? AND status = 'reserved'",
            (int(usage_id),),
        )
        db.commit()
        return cursor.rowcount == 1


async def abandon_ai_usage_before_provider(db_path: str, usage_id: int, reason: str) -> bool:
    """Release a reservation only when AutoDoctor knows no provider call started."""
    return await asyncio.to_thread(
        _abandon_ai_usage_before_provider_sync,
        db_path,
        usage_id,
        reason,
    )


def _abandon_ai_usage_before_provider_sync(db_path: str, usage_id: int, reason: str) -> bool:
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            """UPDATE ai_usage
            SET status = 'abandoned_pre_provider', cost_usd = 0, error = ?
            WHERE id = ? AND status = 'reserved'""",
            (str(reason)[:1000], int(usage_id)),
        )
        db.commit()
        return cursor.rowcount == 1


async def recover_orphaned_ai_usage(db_path: str) -> AIUsageRecoveryResult:
    """Reconcile AI reservations left behind by a previous AutoDoctor process.

    v0.4.3 introduces an explicit ``inflight`` state immediately before entering the
    provider call. This lets future restarts distinguish reservations that were never
    sent to the provider (safe to release) from calls whose provider outcome is unknown
    (reservation retained conservatively).

    Existing pre-v0.4.3 ``reserved`` rows are ambiguous because older versions did not
    record the transition. On the first v0.4.3 startup they are therefore retained and
    labelled ``abandoned_legacy_unknown`` rather than being under-counted.
    """
    return await asyncio.to_thread(_recover_orphaned_ai_usage_sync, db_path)


def _sum_for_status(db: sqlite3.Connection, status: str) -> tuple[int, float]:
    row = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE status = ?",
        (status,),
    ).fetchone()
    return int(row[0]), float(row[1])


def _recover_orphaned_ai_usage_sync(db_path: str) -> AIUsageRecoveryResult:
    with sqlite3.connect(db_path) as db:
        db.executescript(_META_SCHEMA)
        initialized = db.execute(
            "SELECT value FROM autodoctor_meta WHERE key = ?",
            (_LIFECYCLE_KEY,),
        ).fetchone()

        inflight_count, inflight_cost = _sum_for_status(db, "inflight")
        if inflight_count:
            db.execute(
                """UPDATE ai_usage
                SET status = 'abandoned_inflight',
                    error = 'AutoDoctor restarted while provider outcome was unknown; reservation retained conservatively'
                WHERE status = 'inflight'"""
            )

        if initialized is None:
            legacy_count, legacy_cost = _sum_for_status(db, "reserved")
            if legacy_count:
                db.execute(
                    """UPDATE ai_usage
                    SET status = 'abandoned_legacy_unknown',
                        error = 'Pre-v0.4.3 reservation found at startup; provider-start state unknown, reservation retained conservatively'
                    WHERE status = 'reserved'"""
                )
            db.execute(
                "INSERT INTO autodoctor_meta(key, value) VALUES (?, '1')",
                (_LIFECYCLE_KEY,),
            )
            db.commit()
            result = AIUsageRecoveryResult(
                legacy_unknown=legacy_count,
                retained_inflight=inflight_count,
                retained_cost_usd=legacy_cost + inflight_cost,
            )
        else:
            reserved_count, reserved_cost = _sum_for_status(db, "reserved")
            if reserved_count:
                db.execute(
                    """UPDATE ai_usage
                    SET status = 'abandoned_pre_provider', cost_usd = 0,
                        error = 'AutoDoctor restarted before external provider call began; reservation released'
                    WHERE status = 'reserved'"""
                )
            db.commit()
            result = AIUsageRecoveryResult(
                released_pre_provider=reserved_count,
                retained_inflight=inflight_count,
                released_cost_usd=reserved_cost,
                retained_cost_usd=inflight_cost,
            )

    if any((result.legacy_unknown, result.released_pre_provider, result.retained_inflight)):
        _LOG.warning(
            "AI usage startup recovery: legacy_unknown=%d released_pre_provider=%d "
            "retained_inflight=%d released=$%.8f retained=$%.8f",
            result.legacy_unknown,
            result.released_pre_provider,
            result.retained_inflight,
            result.released_cost_usd,
            result.retained_cost_usd,
        )
    else:
        _LOG.info("AI usage startup recovery found no orphaned reservations")
    return result
