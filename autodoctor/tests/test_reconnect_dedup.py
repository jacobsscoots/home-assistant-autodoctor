from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.config import Settings
from autodoctor.engine import AutoDoctorEngine
from autodoctor.fingerprint import fingerprint
from autodoctor.models import LogEvent

_PATTERN_KEY = "kasa/authentication/a8f6419a8c"
_SOURCE = "['components/tplink/coordinator.py', 78]"


def kasa_event(
    terminal_uuid: str,
    sequence: int,
    *,
    final_method: str = "get_energy_usage",
) -> LogEvent:
    message = (
        "Query failed after successful authentication: Host is 192.168.50.80, "
        f"Sequence is {sequence}, Response status is 403, Request was "
        '{"method":"multipleRequest","request_time_milis":1788270000000,'
        f'"terminal_uuid":"{terminal_uuid}","params":{{"requests":['
        '{"method":"get_device_time"},{"method":"get_auto_off_config",'
        '"params":{"start_index":0}},{"method":"get_device_info"},'
        '{"method":"get_device_usage"},'
        f'{{"method":"{final_method}"}}]}}}}'
    )
    return LogEvent(
        "ERROR",
        _SOURCE,
        "",
        message,
        "kasa.transports.klaptransport",
        datetime.now(tz=timezone.utc).timestamp(),
    )


def test_kasa_session_token_and_sequence_sign_do_not_split_fingerprint() -> None:
    before = fingerprint(kasa_event("nyjtYLAmWJiBEb9Ph8alVw==", -1356530975))
    after = fingerprint(kasa_event("xj4qlFDK1Ci6r0Ag26P6KA==", 1245713777))
    assert before == after


def test_kasa_material_request_change_still_splits_fingerprint() -> None:
    baseline = fingerprint(kasa_event("sameSessionToken==", 10))
    changed = fingerprint(
        kasa_event("differentSessionToken==", -20, final_method="get_device_running_info")
    )
    assert baseline != changed


class FakeStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def list_recent(self, limit: int) -> list[dict]:
        return self.rows[:limit]

    async def ai_count_since(self, since_ts: float) -> int:
        return 0

    async def ai_count_for_family_since(self, family: str, since_ts: float) -> int:
        return 0


def engine_for(rows: list[dict]) -> AutoDoctorEngine:
    settings = Settings(
        min_occurrences_for_ai=1,
        analysis_cooldown_seconds=1800,
        max_ai_analyses_per_hour=6,
        max_ai_analyses_per_family_per_hour=2,
        ai_startup_backlog_grace_seconds=0,
    )
    return AutoDoctorEngine(settings, FakeStore(rows), None, None, None)  # type: ignore[arg-type]


def candidate_row(now: float) -> dict:
    return {
        "first_seen": now,
        "occurrences": 1,
        "last_analysis_at": None,
        "pattern_key": _PATTERN_KEY,
    }


def test_pattern_cooldown_suppresses_fresh_fingerprint_after_recent_same_pattern() -> None:
    async def run() -> None:
        now = datetime.now(tz=timezone.utc).timestamp()
        engine = engine_for(
            [{"pattern_key": _PATTERN_KEY, "last_analysis_at": now - 76.0}]
        )
        allowed = await engine._should_analyze(
            kasa_event("newSessionToken==", 123),
            candidate_row(now),
            "kasa",
        )
        assert not allowed
        assert engine.pattern_deferred == 1

    asyncio.run(run())


def test_pattern_cooldown_allows_analysis_after_window_expires() -> None:
    async def run() -> None:
        now = datetime.now(tz=timezone.utc).timestamp()
        engine = engine_for(
            [{"pattern_key": _PATTERN_KEY, "last_analysis_at": now - 1801.0}]
        )
        allowed = await engine._should_analyze(
            kasa_event("newSessionToken==", 123),
            candidate_row(now),
            "kasa",
        )
        assert allowed
        assert engine.pattern_deferred == 0

    asyncio.run(run())


def test_pattern_attempt_cache_survives_fingerprint_change_in_same_process() -> None:
    async def run() -> None:
        now = datetime.now(tz=timezone.utc).timestamp()
        engine = engine_for([])
        engine._pattern_analysis_loaded = True
        engine._mark_pattern_analysis_attempt(_PATTERN_KEY, now - 10.0)
        allowed = await engine._should_analyze(
            kasa_event("anotherSessionToken==", -456),
            candidate_row(now),
            "kasa",
        )
        assert not allowed
        assert engine.pattern_deferred == 1

    asyncio.run(run())
