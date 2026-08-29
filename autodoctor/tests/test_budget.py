from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.budget import (
    conservative_input_tokens,
    estimate_cost_usd,
    month_bounds_utc,
    reservation_for_prompt,
    validate_ai_budget,
)
from autodoctor.config import Settings
from autodoctor.store import IncidentStore


def valid_settings(**overrides):
    values = dict(
        ai_provider="openai",
        ai_model="test-model",
        openai_api_key="test-key",
        ai_budget_enabled=True,
        ai_monthly_budget_usd=5.0,
        ai_monthly_stop_usd=4.5,
        ai_input_cost_per_million_usd=0.2,
        ai_output_cost_per_million_usd=1.2,
    )
    values.update(overrides)
    return Settings(**values)


def test_budget_validation_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="budget guard must be enabled"):
        validate_ai_budget(Settings(ai_provider="openai", ai_model="x"))

    with pytest.raises(RuntimeError, match="lower than"):
        validate_ai_budget(valid_settings(ai_monthly_stop_usd=5.0))

    with pytest.raises(RuntimeError, match="input_cost"):
        validate_ai_budget(valid_settings(ai_input_cost_per_million_usd=0.0))

    validate_ai_budget(valid_settings())


def test_none_provider_does_not_require_budget() -> None:
    validate_ai_budget(Settings(ai_provider="none"))


def test_conservative_reservation_and_cost() -> None:
    settings = valid_settings()
    prompt = "hello"
    reservation = reservation_for_prompt(prompt, 4000, settings)
    assert reservation.input_tokens == len(prompt.encode("utf-8")) + 512
    assert reservation.output_tokens == 4000
    expected = estimate_cost_usd(517, 4000, 0.2, 1.2)
    assert reservation.cost_usd == expected
    assert conservative_input_tokens("é") == len("é".encode("utf-8")) + 512


def test_month_bounds_use_calendar_month_utc() -> None:
    # 2026-08-29T12:00:00Z
    start, end, month = month_bounds_utc(1788004800.0)
    assert month == "2026-08"
    assert end > start
    assert end - start == 31 * 24 * 3600


def test_legacy_ai_usage_migrates(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ai_usage (ts REAL NOT NULL, fingerprint TEXT NOT NULL)")
        db.execute("INSERT INTO ai_usage(ts, fingerprint) VALUES (?, ?)", (1788004800.0, "legacy"))
        db.commit()

    store = IncidentStore(str(path))
    asyncio.run(store.initialize())

    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(ai_usage)").fetchall()}
        assert {"id", "provider", "model", "status", "cost_usd"}.issubset(columns)
        row = db.execute("SELECT status, fingerprint FROM ai_usage").fetchone()
        assert row == ("legacy_success", "legacy")


def test_budget_validation_rejects_nan_and_infinity() -> None:
    with pytest.raises(RuntimeError, match="finite"):
        validate_ai_budget(valid_settings(ai_monthly_stop_usd=float("nan")))
    with pytest.raises(RuntimeError, match="finite"):
        validate_ai_budget(valid_settings(ai_input_cost_per_million_usd=float("inf")))


def test_budget_usage_resets_by_calendar_month_and_survives_reopen(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "persistent.db"
        store = IncidentStore(str(path))
        await store.initialize()
        usage_id, _ = await store.reserve_ai_usage(
            fingerprint="aug",
            provider="openai",
            model="model",
            reserved_input_tokens=100,
            reserved_output_tokens=100,
            reserved_cost_usd=0.25,
            monthly_stop_usd=1.0,
            now_ts=1788004800.0,
        )
        assert usage_id is not None
        await store.fail_ai_usage(usage_id, "unknown provider outcome")

        reopened = IncidentStore(str(path))
        await reopened.initialize()
        august = await reopened.monthly_ai_usage(1788004900.0)
        assert august["spent_usd"] == pytest.approx(0.25)
        assert august["failed_count"] == 1

        # 2026-09-01T12:00:00Z
        september = await reopened.monthly_ai_usage(1788264000.0)
        assert september["month_utc"] == "2026-09"
        assert september["spent_usd"] == 0
        assert september["attempts_count"] == 0

    asyncio.run(run())


def test_analysis_attempt_timestamp_persists(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "attempt.db"
        store = IncidentStore(str(path))
        await store.initialize()
        from autodoctor.models import LogEvent

        event = LogEvent("ERROR", "core.py", "", "boom", "test", 1788004800.0)
        await store.record("fp", event)
        await store.mark_analysis_attempt("fp", 1788004900.0)
        row = (await store.list_recent(1))[0]
        assert row["last_analysis_at"] == 1788004900.0

        reopened = IncidentStore(str(path))
        await reopened.initialize()
        row = (await reopened.list_recent(1))[0]
        assert row["last_analysis_at"] == 1788004900.0

    asyncio.run(run())


def test_failed_call_keeps_reservation_and_blocks_next_call(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "budget.db"
        store = IncidentStore(str(path))
        await store.initialize()

        usage_id, spent = await store.reserve_ai_usage(
            fingerprint="fp1",
            provider="openai",
            model="model",
            reserved_input_tokens=100,
            reserved_output_tokens=4000,
            reserved_cost_usd=0.30,
            monthly_stop_usd=0.50,
            now_ts=1788004800.0,
        )
        assert usage_id is not None
        assert spent == 0
        await store.fail_ai_usage(usage_id, "network failed")

        blocked_id, spent = await store.reserve_ai_usage(
            fingerprint="fp2",
            provider="openai",
            model="model",
            reserved_input_tokens=100,
            reserved_output_tokens=4000,
            reserved_cost_usd=0.25,
            monthly_stop_usd=0.50,
            now_ts=1788004900.0,
        )
        assert blocked_id is None
        assert spent == pytest.approx(0.30)

        summary = await store.monthly_ai_usage(1788004900.0)
        assert summary["spent_usd"] == pytest.approx(0.30)
        assert summary["failed_count"] == 1
        assert summary["budget_blocked_count"] == 1
        assert summary["attempts_count"] == 1

    asyncio.run(run())


def test_success_replaces_reservation_with_actual_usage(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "success.db"
        store = IncidentStore(str(path))
        await store.initialize()

        usage_id, _ = await store.reserve_ai_usage(
            fingerprint="fp",
            provider="openai",
            model="model",
            reserved_input_tokens=1000,
            reserved_output_tokens=4000,
            reserved_cost_usd=0.20,
            monthly_stop_usd=1.0,
            now_ts=1788004800.0,
        )
        assert usage_id is not None
        await store.finalize_ai_usage(
            usage_id,
            input_tokens=120,
            output_tokens=80,
            cost_usd=0.01,
        )

        summary = await store.monthly_ai_usage(1788004900.0)
        assert summary["spent_usd"] == pytest.approx(0.01)
        assert summary["analyses_count"] == 1
        assert summary["attempts_count"] == 1
        assert summary["input_tokens"] == 120
        assert summary["output_tokens"] == 80

    asyncio.run(run())
