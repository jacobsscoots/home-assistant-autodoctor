from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.automatic_repair import AutoApplyRepairExecutor
from autodoctor.cases import IncidentCaseManager
from autodoctor.config import Settings
from autodoctor.models import Analysis, LogEvent
from autodoctor.store import IncidentStore

_TARGET = "entry_abc123"
_PATTERN = "integration/test/automatic-reload"


class FakeHA:
    def __init__(self) -> None:
        self.reload_calls: list[str] = []
        self.dismissed: list[str] = []

    async def reload_config_entry(self, entry_id: str) -> None:
        self.reload_calls.append(entry_id)

    async def notify(self, _title: str, _message: str, _notification_id: str) -> None:
        return None

    async def dismiss_notification(self, notification_id: str) -> None:
        self.dismissed.append(notification_id)


class FakeMCP:
    async def call_readonly(self, _tool, arguments=None, *, purpose=""):
        _ = purpose
        return {
            "success": True,
            "entry_id": _TARGET,
            "entry": {
                "entry_id": str((arguments or {}).get("entry_id") or _TARGET),
                "state": "loaded",
                "domain": "test",
            },
        }


async def _build(tmp_path: Path, *, auto_apply: bool):
    db_path = str(tmp_path / "autodoctor.db")
    store = IncidentStore(db_path)
    await store.initialize()
    ha = FakeHA()
    cases = IncidentCaseManager(db_path, ha)
    await cases.initialize()
    await cases.record_event(
        pattern_key=_PATTERN,
        pattern_label="automatic integration reload candidate",
        family="test",
        fingerprint="fp-auto",
        event=LogEvent(
            level="ERROR",
            source="test",
            exception="",
            message="config entry failed",
            name="test.integration",
            timestamp=cases._now() - 300,
        ),
        fingerprint_is_new=True,
    )
    settings = Settings(
        repair_executor_enabled=True,
        auto_apply_low_risk=auto_apply,
        repair_verification_seconds=30,
    )
    executor = AutoApplyRepairExecutor(settings, db_path, ha, FakeMCP(), cases)
    await executor.initialize()
    return cases, executor, ha


async def _make_plan(cases: IncidentCaseManager):
    analysis = Analysis(
        summary="Reload the exact unhealthy config entry.",
        root_cause="The config entry is stuck and a reload is appropriate.",
        confidence=0.97,
        risk="low",
        action="propose_fix",
        checks=["Confirm exact config entry in read-only evidence"],
        proposed_changes=[
            {
                "operation": "reload_config_entry",
                "target": _TARGET,
                "reason": "recover exact unhealthy config entry",
            }
        ],
    )
    plan = await cases.apply_analysis(
        pattern_key=_PATTERN,
        fingerprint="fp-auto",
        analysis=analysis,
        evidence={"reads": [{"result": {"entry_id": _TARGET}}]},
    )
    assert plan is not None
    return plan


def test_auto_execute_requires_explicit_setting(tmp_path: Path) -> None:
    async def run() -> None:
        cases, executor, ha = await _build(tmp_path, auto_apply=False)
        plan = await _make_plan(cases)
        with pytest.raises(PermissionError, match="automatic repair is disabled"):
            await executor.auto_execute(plan["plan_id"])
        assert ha.reload_calls == []
        await executor.close()

    asyncio.run(run())


def test_auto_execute_uses_normal_gates_verifies_and_records_provenance(tmp_path: Path) -> None:
    async def run() -> None:
        cases, executor, ha = await _build(tmp_path, auto_apply=True)
        executor.verification_seconds = 0.05
        plan = await _make_plan(cases)
        result = await executor.auto_execute(plan["plan_id"])
        assert result["status"] == "verifying"
        assert ha.reload_calls == [_TARGET]
        await asyncio.sleep(0.20)

        stored = await executor.get_plan(plan["plan_id"])
        assert stored is not None
        assert stored["status"] == "succeeded"

        with sqlite3.connect(executor.db_path) as db:
            execution = db.execute(
                "SELECT execution_mode, status FROM repair_executions WHERE execution_id=?",
                (result["execution_id"],),
            ).fetchone()
            knowledge = db.execute(
                "SELECT source, verification FROM knowledge WHERE memory_key=?",
                (f"repair:{plan['plan_id']}",),
            ).fetchone()
        assert execution == ("automatic", "succeeded")
        assert knowledge is not None
        assert knowledge[0] == "autodoctor-auto-repair"
        assert "Automatically applied" in knowledge[1]
        await executor.close()

    asyncio.run(run())


def test_competing_executors_cannot_reload_the_same_plan_twice(tmp_path: Path) -> None:
    async def run() -> None:
        cases, first, ha = await _build(tmp_path, auto_apply=True)
        second = AutoApplyRepairExecutor(
            Settings(repair_executor_enabled=True, auto_apply_low_risk=True),
            first.db_path, ha, FakeMCP(), cases,
        )
        await second.initialize()
        plan = await _make_plan(cases)
        try:
            results = await asyncio.gather(
                first.auto_execute(plan["plan_id"]),
                second.auto_execute(plan["plan_id"]),
                return_exceptions=True,
            )
            assert sum(isinstance(result, dict) for result in results) == 1
            assert sum(isinstance(result, (PermissionError, RuntimeError)) for result in results) == 1
            assert ha.reload_calls == [_TARGET]
            from autodoctor.database import database_connection
            with database_connection(first.db_path, readonly=True) as db:
                assert db.execute("SELECT COUNT(*) FROM repair_executions").fetchone()[0] == 1
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())


def test_database_failure_before_execution_never_calls_home_assistant(tmp_path: Path, monkeypatch) -> None:
    from contextlib import closing

    async def run() -> None:
        cases, executor, ha = await _build(tmp_path, auto_apply=True)
        plan = await _make_plan(cases)
        original_connect = sqlite3.connect

        def short_timeout_connect(*args, **kwargs):
            kwargs["timeout"] = 0.02
            return original_connect(*args, **kwargs)

        try:
            with closing(original_connect(executor.db_path)) as external:
                external.execute("BEGIN EXCLUSIVE")
                with monkeypatch.context() as patch:
                    patch.setattr(sqlite3, "connect", short_timeout_connect)
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        await executor.auto_execute(plan["plan_id"])
                assert ha.reload_calls == []
                external.rollback()
            stored = await executor.get_plan(plan["plan_id"])
            assert stored is not None
            assert stored["status"] == "proposed"
        finally:
            await executor.close()

    asyncio.run(run())
