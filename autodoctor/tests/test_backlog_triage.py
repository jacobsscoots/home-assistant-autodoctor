from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

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


def _event(ts: float = 1000.0) -> LogEvent:
    return LogEvent(
        level="ERROR",
        name="kasa.auth",
        source="components/kasa",
        message="Authentication failed for switch.office",
        exception="",
        timestamp=ts,
    )


def test_backlog_cycle_reuses_persisted_event_without_incrementing_incident(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "autodoctor.db")
        store = IncidentStore(db_path)
        await store.initialize()
        event = _event()
        await store.record("fp1", event, "kasa/authentication/abc", "authentication")

        settings = Settings(
            case_backlog_triage_enabled=True,
            case_backlog_triage_max_per_cycle=1,
            ai_startup_backlog_grace_seconds=0,
        )
        engine = CaseAwareAutoDoctorEngine(settings, store, FakeHA(), DummyLLM(), DummyMCP())
        await engine.cases.initialize()
        await engine.cases.record_event(
            pattern_key="kasa/authentication/abc",
            pattern_label="authentication",
            family="kasa",
            fingerprint="fp1",
            event=event,
            fingerprint_is_new=True,
        )

        async def should_analyze(event_arg, row_arg, family_arg):
            assert event_arg.message == event.message
            assert row_arg["fingerprint"] == "fp1"
            assert family_arg == "kasa"
            return True

        captured = []

        async def analyze_persisted(**kwargs):
            captured.append(kwargs)
            return True

        engine._should_analyze = should_analyze
        engine._analyze_persisted_incident = analyze_persisted

        before = (await store.list_recent(10))[0]
        completed = await engine.run_backlog_triage_cycle()
        after = (await store.list_recent(10))[0]

        assert completed == 1
        assert engine.backlog_triage_analyses == 1
        assert len(captured) == 1
        assert captured[0]["source"] == "backlog_triage"
        assert captured[0]["fp"] == "fp1"
        assert before["occurrences"] == 1
        assert after["occurrences"] == 1
        assert before["last_seen"] == after["last_seen"]

    asyncio.run(run())


def test_backlog_triage_only_selects_new_or_reopened_cases(tmp_path: Path) -> None:
    async def run() -> None:
        db_path = str(tmp_path / "autodoctor.db")
        store = IncidentStore(db_path)
        await store.initialize()
        settings = Settings(case_backlog_triage_enabled=True)
        engine = CaseAwareAutoDoctorEngine(settings, store, FakeHA(), DummyLLM(), DummyMCP())
        await engine.cases.initialize()

        statuses = [
            "new",
            "reopened",
            "diagnosed",
            "repair_available",
            "needs_user_action",
            "verifying",
            "historical",
            "resolved",
        ]
        for index, status in enumerate(statuses):
            key = f"pattern/{status}"
            await engine.cases.record_event(
                pattern_key=key,
                pattern_label=status,
                family="test",
                fingerprint=f"fp{index}",
                event=_event(1000 + index),
                fingerprint_is_new=True,
            )
            await engine.cases._set_status(key, status)

        eligible = await engine._eligible_backlog_cases()
        assert {case["status"] for case in eligible} == {"new", "reopened"}
        assert {case["pattern_key"] for case in eligible} == {"pattern/new", "pattern/reopened"}

    asyncio.run(run())


def test_pattern_claim_prevents_live_and_backlog_analysis_race(tmp_path: Path) -> None:
    async def run() -> None:
        store = IncidentStore(str(tmp_path / "autodoctor.db"))
        settings = Settings(case_backlog_triage_enabled=True)
        engine = CaseAwareAutoDoctorEngine(settings, store, FakeHA(), DummyLLM(), DummyMCP())

        assert await engine._claim_pattern("pattern/x") is True
        assert await engine._claim_pattern("pattern/x") is False
        await engine._release_pattern("pattern/x")
        assert await engine._claim_pattern("pattern/x") is True
        await engine._release_pattern("pattern/x")

    asyncio.run(run())


def test_backlog_triage_defaults_are_conservative() -> None:
    settings = Settings()
    assert settings.case_backlog_triage_enabled is True
    assert settings.case_backlog_triage_interval_seconds == 60
    assert settings.case_backlog_triage_max_per_cycle == 1
