from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.config import Settings
from autodoctor.engine import AutoDoctorEngine
from autodoctor.models import LogEvent
from autodoctor.scheduler import incident_family
from autodoctor.store import IncidentStore


def event(message: str = "Setup failed for integration foo", name: str = "homeassistant.components.foo") -> LogEvent:
    return LogEvent("ERROR", "core.py", "", message, name, datetime.now(tz=timezone.utc).timestamp())


class FakeStore:
    def __init__(self, *, global_count: int = 0, family_count: int = 0) -> None:
        self.global_count = global_count
        self.family_count = family_count

    async def ai_count_since(self, since_ts: float) -> int:
        return self.global_count

    async def ai_count_for_family_since(self, family: str, since_ts: float) -> int:
        return self.family_count


def engine_for(store: FakeStore, **overrides) -> AutoDoctorEngine:
    values = dict(
        min_occurrences_for_ai=2,
        analysis_cooldown_seconds=1800,
        max_ai_analyses_per_hour=6,
        max_ai_analyses_per_family_per_hour=2,
        ai_startup_backlog_grace_seconds=300,
    )
    values.update(overrides)
    return AutoDoctorEngine(Settings(**values), store, None, None, None)  # type: ignore[arg-type]


def test_incident_family_groups_noisy_logger_children() -> None:
    assert incident_family("kasa.smart.smartdevice") == "kasa"
    assert incident_family("kasa.protocol") == "kasa"
    assert incident_family("homeassistant.components.tplink.coordinator") == "homeassistant.components.tplink"
    assert incident_family("homeassistant.components.mqtt.sensor") == "homeassistant.components.mqtt"
    assert incident_family("custom_components.example.sensor") == "custom_components.example"
    assert incident_family("", "library.worker") == "library"
    assert incident_family("", "") == "unknown"


def test_startup_backlog_guard_defers_old_incident_but_allows_new_incident() -> None:
    async def run() -> None:
        store = FakeStore()
        engine = engine_for(store)
        now = datetime.now(tz=timezone.utc).timestamp()
        engine.started_at = now

        old_row = {
            "first_seen": now - 60,
            "occurrences": 10,
            "last_analysis_at": None,
        }
        assert not await engine._should_analyze(event(), old_row, "kasa")
        assert engine.backlog_deferred == 1

        new_row = {
            "first_seen": now + 0.001,
            "occurrences": 1,
            "last_analysis_at": None,
        }
        assert await engine._should_analyze(event(), new_row, "homeassistant.components.foo")

        # A non-immediate incident first seen after startup can reach the normal
        # occurrence threshold during the grace period instead of being mistaken
        # for old backlog on its second event.
        repeated_new = {
            "first_seen": now + 0.001,
            "occurrences": 2,
            "last_analysis_at": None,
        }
        assert await engine._should_analyze(
            event("Ordinary repeated failure"),
            repeated_new,
            "homeassistant.components.foo",
        )

    asyncio.run(run())


def test_family_cap_preserves_global_capacity_for_other_families() -> None:
    async def run() -> None:
        engine = engine_for(FakeStore(global_count=2, family_count=2), ai_startup_backlog_grace_seconds=0)
        row = {"first_seen": 1.0, "occurrences": 5, "last_analysis_at": None}
        assert not await engine._should_analyze(event(), row, "kasa")
        assert engine.family_deferred == 1
        assert engine.hourly_deferred == 0

    asyncio.run(run())


def test_global_hourly_cap_remains_independent() -> None:
    async def run() -> None:
        engine = engine_for(FakeStore(global_count=6, family_count=0), ai_startup_backlog_grace_seconds=0)
        row = {"first_seen": 1.0, "occurrences": 5, "last_analysis_at": None}
        assert not await engine._should_analyze(event(), row, "kasa")
        assert engine.hourly_deferred == 1
        assert engine.family_deferred == 0

    asyncio.run(run())


def test_v012_usage_schema_migrates_in_place_and_backfills_family(tmp_path: Path) -> None:
    path = tmp_path / "v012.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE incidents (
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
            CREATE TABLE ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                fingerprint TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
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
        )
        db.execute(
            """INSERT INTO incidents
            (fingerprint, first_seen, last_seen, occurrences, level, name, source, message, exception, status)
            VALUES ('fp', 1, 2, 3, 'ERROR', 'kasa.smart.smartdevice', 'core.py', 'timeout', '', 'open')"""
        )
        db.execute(
            """INSERT INTO ai_usage
            (ts, fingerprint, provider, model, status, reserved_input_tokens,
             reserved_output_tokens, input_tokens, output_tokens,
             reserved_cost_usd, cost_usd, error)
            VALUES (1000, 'fp', 'openai', 'model', 'succeeded', 900, 4000, 120, 80, 0.02, 0.001, NULL)"""
        )
        db.commit()

    store = IncidentStore(str(path))
    asyncio.run(store.initialize())

    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(ai_usage)").fetchall()}
        assert "family" in columns
        row = db.execute(
            "SELECT family, status, input_tokens, output_tokens, cost_usd FROM ai_usage WHERE fingerprint = 'fp'"
        ).fetchone()
        assert row == ("kasa", "succeeded", 120, 80, 0.001)


def test_unknown_usage_schema_fails_closed_instead_of_rebuilding(tmp_path: Path) -> None:
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ai_usage (ts REAL, fingerprint TEXT, mystery TEXT)")
        db.execute("INSERT INTO ai_usage VALUES (1, 'fp', 'keep-me')")
        db.commit()

    store = IncidentStore(str(path))
    initialize = store.initialize()
    with pytest.raises(RuntimeError, match="refusing destructive migration"):
        asyncio.run(initialize)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT mystery FROM ai_usage").fetchone()[0] == "keep-me"


def test_store_family_count_tracks_attempts_by_family(tmp_path: Path) -> None:
    async def run() -> None:
        store = IncidentStore(str(tmp_path / "families.db"))
        await store.initialize()
        for fp, family, ts in (
            ("k1", "kasa", 1000.0),
            ("k2", "kasa", 1100.0),
            ("m1", "homeassistant.components.mqtt", 1200.0),
        ):
            usage_id, _ = await store.reserve_ai_usage(
                fingerprint=fp,
                provider="openai",
                model="model",
                family=family,
                reserved_input_tokens=100,
                reserved_output_tokens=100,
                reserved_cost_usd=0.01,
                monthly_stop_usd=1.0,
                now_ts=ts,
            )
            assert usage_id is not None

        assert await store.ai_count_for_family_since("kasa", 900.0) == 2
        assert await store.ai_count_for_family_since("homeassistant.components.mqtt", 900.0) == 1
        assert await store.ai_count_for_family_since("kasa", 1050.0) == 1
        assert await store.ai_family_counts_since(900.0) == {
            "kasa": 2,
            "homeassistant.components.mqtt": 1,
        }

    asyncio.run(run())
