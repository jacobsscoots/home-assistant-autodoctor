from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.ai_usage_recovery import (
    abandon_ai_usage_before_provider,
    mark_ai_usage_inflight,
    recover_orphaned_ai_usage,
)


SCHEMA = """
CREATE TABLE ai_usage (
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
"""


def _db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.commit()


def _insert(path: Path, status: str, cost: float) -> int:
    with sqlite3.connect(path) as db:
        cur = db.execute(
            """INSERT INTO ai_usage
            (ts, fingerprint, provider, model, family, status,
             reserved_input_tokens, reserved_output_tokens, reserved_cost_usd, cost_usd)
            VALUES (1000, 'fp', 'openai', 'model', 'family', ?, 100, 100, ?, ?)""",
            (status, cost, cost),
        )
        db.commit()
        return int(cur.lastrowid)


def _row(path: Path, usage_id: int) -> tuple[str, float, str | None]:
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT status, cost_usd, error FROM ai_usage WHERE id = ?",
            (usage_id,),
        ).fetchone()


def test_first_v043_start_keeps_legacy_unknown_reservation_conservative(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "usage.db"
        _db(path)
        usage_id = _insert(path, "reserved", 0.009)

        result = await recover_orphaned_ai_usage(str(path))
        assert result.legacy_unknown == 1
        assert result.released_pre_provider == 0
        assert result.retained_cost_usd == 0.009
        status, cost, error = _row(path, usage_id)
        assert status == "abandoned_legacy_unknown"
        assert cost == 0.009
        assert "unknown" in str(error).lower()

        again = await recover_orphaned_ai_usage(str(path))
        assert again.legacy_unknown == 0
        assert again.released_pre_provider == 0
        assert again.retained_inflight == 0
        assert _row(path, usage_id)[0] == "abandoned_legacy_unknown"

    asyncio.run(run())


def test_future_restart_releases_pre_provider_and_retains_inflight(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "usage.db"
        _db(path)
        await recover_orphaned_ai_usage(str(path))  # installs lifecycle marker

        reserved_id = _insert(path, "reserved", 0.012)
        inflight_id = _insert(path, "inflight", 0.021)
        result = await recover_orphaned_ai_usage(str(path))

        assert result.released_pre_provider == 1
        assert result.retained_inflight == 1
        assert result.released_cost_usd == 0.012
        assert result.retained_cost_usd == 0.021
        assert _row(path, reserved_id)[0:2] == ("abandoned_pre_provider", 0.0)
        assert _row(path, inflight_id)[0:2] == ("abandoned_inflight", 0.021)

    asyncio.run(run())


def test_provider_transition_is_atomic_and_pre_provider_abort_is_releasable(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "usage.db"
        _db(path)
        usage_id = _insert(path, "reserved", 0.01)
        assert await mark_ai_usage_inflight(str(path), usage_id) is True
        assert _row(path, usage_id)[0] == "inflight"
        assert await mark_ai_usage_inflight(str(path), usage_id) is False
        assert await abandon_ai_usage_before_provider(str(path), usage_id, "not applicable") is False
        assert _row(path, usage_id)[0:2] == ("inflight", 0.01)

        second_id = _insert(path, "reserved", 0.02)
        assert await abandon_ai_usage_before_provider(str(path), second_id, "pre-provider failure") is True
        assert _row(path, second_id)[0:2] == ("abandoned_pre_provider", 0.0)

    asyncio.run(run())
