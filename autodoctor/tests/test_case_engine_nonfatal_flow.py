from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_engine import CaseAwareAutoDoctorEngine
from autodoctor.models import LogEvent


class DummySettings:
    notify_on_new_incident = True
    memory_enabled = True
    case_backlog_triage_enabled = False
    max_incidents_retained = 5000
    case_backlog_triage_interval_seconds = 60
    case_backlog_triage_max_per_cycle = 1


class DummyStore:
    path = ":memory:"


class DummyHA:
    pass


class DummyLLM:
    pass


class DummyMCP:
    pass


def _event() -> LogEvent:
    return LogEvent(
        level="ERROR",
        name="kasa.protocol",
        source="['components/tplink/coordinator.py', 78]",
        message="sub-call failed",
        exception="",
        timestamp=1000.0,
    )


def test_nonfatal_live_event_is_retained_but_skips_memory_ai_and_notification() -> None:
    async def run() -> None:
        engine = object.__new__(CaseAwareAutoDoctorEngine)
        engine.settings = DummySettings()
        engine.processed_events = 0
        engine.nonfatal_events_suppressed = 0
        engine.llm = DummyLLM()

        calls: list[str] = []

        async def record_incident(event):
            _ = event
            calls.append("record_incident")
            return (
                "fp",
                "kasa",
                "kasa/device_query_timeout/x",
                "device_query_timeout",
                {"occurrences": 10},
                False,
            )

        class Cases:
            async def record_event(self, **kwargs):
                _ = kwargs
                calls.append("record_case")
                return {"status": "diagnosed"}, False

            async def mark_suppressed_nonfatal(self, pattern_key, reason):
                _ = pattern_key, reason
                calls.append("suppress")
                return True

            async def reopen_if_suppressed(self, pattern_key):
                _ = pattern_key
                calls.append("reopen")
                return False

            async def publish_case(self, pattern_key, *, force=False):
                _ = pattern_key, force
                calls.append("notify")
                return True

        async def memory_feedback(*args, **kwargs):
            _ = args, kwargs
            calls.append("memory")

        async def should_analyze(*args, **kwargs):
            _ = args, kwargs
            calls.append("analyze_gate")
            return True

        async def analyze(**kwargs):
            _ = kwargs
            calls.append("ai")
            return True

        engine.cases = Cases()
        engine._record_incident = record_incident
        engine._record_memory_feedback = memory_feedback
        engine._should_analyze = should_analyze
        engine._analyze_persisted_incident = analyze
        engine._should_process_event = lambda event: True

        await engine.handle_event(_event())

        assert calls == ["record_incident", "record_case", "memory", "suppress"]
        assert engine.nonfatal_events_suppressed == 1
        assert engine.processed_events == 1
        assert "notify" not in calls
        assert "analyze_gate" not in calls
        assert "ai" not in calls

    asyncio.run(run())
