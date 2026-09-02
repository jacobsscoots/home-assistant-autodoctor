from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_consistency import retire_orphaned_active_case
from autodoctor.case_engine import CaseAwareAutoDoctorEngine
from autodoctor.config import Settings
from autodoctor.models import LogEvent
from autodoctor.store import IncidentStore


class FakeHA:
    async def notify(self, title: str, message: str, notification_id: str) -> None:
        return None

    async def dismiss_notification(self, notification_id: str) -> None:
        return None


class DummyLLM:
    provider_name = "dummy"
    model = "dummy"
    max_output_tokens = 100


class DummyMCP:
    pass


def _event(message: str, ts: float) -> LogEvent:
    return LogEvent(
        level="ERROR",
        name="kasa.auth",
        source="components/kasa",
        message=message,
        exception="",
        timestamp=ts,
    )


def test_stale_representative_falls_back_to_newest_matching_incident(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "autodoctor.db")
        store = IncidentStore(db_path)
        await store.initialize()
        settings = Settings(case_backlog_triage_enabled=True)
        engine = CaseAwareAutoDoctorEngine(settings, store, FakeHA(), DummyLLM(), DummyMCP())
        await engine.cases.initialize()

        moved = _event("Authentication failed", 1000.0)
        matching = _event("Rate limited", 1100.0)
        await store.record("fp-moved", moved, "kasa/authentication/b", "authentication")
        await store.record("fp-match", matching, "kasa/rate_limit/a", "rate limit")

        await engine.cases.record_event(
            pattern_key="kasa/rate_limit/a",
            pattern_label="rate limit",
            family="kasa",
            fingerprint="fp-moved",
            event=moved,
            fingerprint_is_new=True,
        )

        incident_rows = await store.list_recent(10)
        by_fp = {str(row["fingerprint"]): row for row in incident_rows}
        by_pattern: dict[str, list[dict]] = {}
        for row in incident_rows:
            by_pattern.setdefault(str(row["pattern_key"]), []).append(row)
        case = await engine.cases.get_case("kasa/rate_limit/a")
        assert case is not None

        before = {row["fingerprint"]: dict(row) for row in incident_rows}
        resolved = await engine._resolve_backlog_incident(case, by_fp, by_pattern)
        after_rows = await store.list_recent(10)
        after = {row["fingerprint"]: dict(row) for row in after_rows}

        assert resolved is not None
        fp, row = resolved
        assert fp == "fp-match"
        assert row["pattern_key"] == "kasa/rate_limit/a"
        assert engine.backlog_triage_representative_fallbacks == 1
        assert engine.backlog_triage_orphaned_cases_retired == 0
        assert before == after
        current = await engine.cases.get_case("kasa/rate_limit/a")
        assert current is not None
        assert current["status"] == "new"

    asyncio.run(run())


def test_orphaned_active_case_is_retired_without_ai_or_incident_mutation(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "autodoctor.db")
        store = IncidentStore(db_path)
        await store.initialize()
        settings = Settings(case_backlog_triage_enabled=True)
        engine = CaseAwareAutoDoctorEngine(settings, store, FakeHA(), DummyLLM(), DummyMCP())
        await engine.cases.initialize()

        moved = _event("Authentication failed", 1000.0)
        await store.record("fp-moved", moved, "kasa/authentication/b", "authentication")
        await engine.cases.record_event(
            pattern_key="kasa/rate_limit/orphan",
            pattern_label="rate limit",
            family="kasa",
            fingerprint="fp-moved",
            event=moved,
            fingerprint_is_new=True,
        )

        incident_rows = await store.list_recent(10)
        by_fp = {str(row["fingerprint"]): row for row in incident_rows}
        by_pattern = {"kasa/authentication/b": incident_rows}
        case = await engine.cases.get_case("kasa/rate_limit/orphan")
        assert case is not None
        before = [dict(row) for row in incident_rows]

        resolved = await engine._resolve_backlog_incident(case, by_fp, by_pattern)

        assert resolved is None
        assert engine.backlog_triage_orphaned_cases_retired == 1
        assert engine.backlog_triage_skipped == 1
        current = await engine.cases.get_case("kasa/rate_limit/orphan")
        assert current is not None
        assert current["status"] == "historical"
        assert before == [dict(row) for row in await store.list_recent(10)]

    asyncio.run(run())


def test_retirement_refuses_case_when_retained_matching_evidence_exists(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "autodoctor.db")
        store = IncidentStore(db_path)
        await store.initialize()
        settings = Settings(case_backlog_triage_enabled=True)
        engine = CaseAwareAutoDoctorEngine(settings, store, FakeHA(), DummyLLM(), DummyMCP())
        await engine.cases.initialize()

        event = _event("Rate limited", 1000.0)
        await store.record("fp-match", event, "kasa/rate_limit/a", "rate limit")
        await engine.cases.record_event(
            pattern_key="kasa/rate_limit/a",
            pattern_label="rate limit",
            family="kasa",
            fingerprint="fp-match",
            event=event,
            fingerprint_is_new=True,
        )

        assert await retire_orphaned_active_case(db_path, "kasa/rate_limit/a") is False
        case = await engine.cases.get_case("kasa/rate_limit/a")
        assert case is not None
        assert case["status"] == "new"

    asyncio.run(run())
