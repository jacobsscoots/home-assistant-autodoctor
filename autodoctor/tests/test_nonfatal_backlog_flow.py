from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.case_engine import CaseAwareAutoDoctorEngine


def test_backlog_nonfatal_case_is_suppressed_before_ai_gate() -> None:
    async def run() -> None:
        engine = object.__new__(CaseAwareAutoDoctorEngine)
        engine.backlog_triage_skipped = 0

        calls: list[str] = []
        row = {
            "fingerprint": "fp",
            "pattern_key": "kasa/device_query_timeout/x",
            "pattern_label": "device_query_timeout",
            "name": "kasa.protocol",
            "source": "['components/tplink/coordinator.py', 78]",
            "message": "transport retry",
            "exception": "",
            "level": "ERROR",
            "last_seen": 1000.0,
            "occurrences": 100,
        }
        case = {
            "pattern_key": row["pattern_key"],
            "pattern_label": row["pattern_label"],
            "representative_fingerprint": "fp",
        }

        async def resolve(*args, **kwargs):
            _ = args, kwargs
            calls.append("resolve")
            return "fp", row

        class Cases:
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

        async def should_analyze(*args, **kwargs):
            _ = args, kwargs
            calls.append("ai_gate")
            return True

        engine.cases = Cases()
        engine._resolve_backlog_incident = resolve
        engine._should_process_event = lambda event: True
        engine._should_analyze = should_analyze

        result = await engine._triage_backlog_case(case, {"fp": row}, {row["pattern_key"]: [row]})
        assert result is False
        assert calls == ["resolve", "suppress"]
        assert engine.backlog_triage_skipped == 1
        assert "ai_gate" not in calls
        assert "notify" not in calls

    asyncio.run(run())
