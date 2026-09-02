from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.cases import IncidentCaseManager
from autodoctor.config import Settings
from autodoctor.models import Analysis, LogEvent
from autodoctor.repair_executor import RepairExecutor
from autodoctor.store import IncidentStore

_TARGET = "entry_abc123"
_PATTERN = "integration/test/reload"


class FakeHA:
    def __init__(self, *, fail_reload: bool = False) -> None:
        self.fail_reload = fail_reload
        self.reload_calls: list[str] = []
        self.notifications: list[tuple[str, str, str]] = []
        self.dismissed: list[str] = []

    async def reload_config_entry(self, entry_id: str) -> None:
        self.reload_calls.append(entry_id)
        if self.fail_reload:
            raise RuntimeError("simulated HA reload failure")

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        self.notifications.append((title, message, notification_id))

    async def dismiss_notification(self, notification_id: str) -> None:
        self.dismissed.append(notification_id)


class FakeMCP:
    def __init__(self, *, state: str = "loaded", fail: bool = False) -> None:
        self.state = state
        self.fail = fail
        self.calls: list[tuple[str, dict, str]] = []

    async def call_readonly(self, tool, arguments=None, *, purpose=""):
        self.calls.append((tool, dict(arguments or {}), purpose))
        if self.fail:
            raise RuntimeError("verification unavailable")
        return {
            "success": True,
            "entry_id": _TARGET,
            "entry": {"entry_id": _TARGET, "state": self.state, "domain": "test"},
        }


async def build_stack(tmp_path: Path, *, enabled: bool = True, ha: FakeHA | None = None, mcp: FakeMCP | None = None):
    db_path = str(tmp_path / "autodoctor.db")
    store = IncidentStore(db_path)
    await store.initialize()
    ha = ha or FakeHA()
    mcp = mcp or FakeMCP()
    cases = IncidentCaseManager(db_path, ha)
    await cases.initialize()
    old_ts = cases._now() - 300
    await cases.record_event(
        pattern_key=_PATTERN,
        pattern_label="integration reload candidate",
        family="test",
        fingerprint="fp-repair",
        event=LogEvent(
            level="ERROR",
            source="test",
            exception="",
            message="config entry failed",
            name="test.integration",
            timestamp=old_ts,
        ),
        fingerprint_is_new=True,
    )
    settings = Settings(
        repair_executor_enabled=enabled,
        repair_verification_seconds=30,
    )
    executor = RepairExecutor(settings, db_path, ha, mcp, cases)
    await executor.initialize()
    return store, cases, executor, ha, mcp


async def make_plan(cases: IncidentCaseManager, *, risk: str = "low", confidence: float = 0.97, evidence=None, changes=None):
    analysis = Analysis(
        summary="Reload the exact unhealthy config entry.",
        root_cause="The config entry is stuck and can be safely reloaded.",
        confidence=confidence,
        risk=risk,
        action="propose_fix",
        checks=["Confirm exact config entry in read-only evidence"],
        proposed_changes=changes
        if changes is not None
        else [
            {
                "operation": "reload_config_entry",
                "target": _TARGET,
                "reason": "recover exact unhealthy config entry",
                "expected_result": "entry returns to loaded",
                "rollback": "no persistent configuration was changed",
                "preconditions": ["target appears unambiguously in MCP evidence"],
            }
        ],
    )
    plan = await cases.apply_analysis(
        pattern_key=_PATTERN,
        fingerprint="fp-repair",
        analysis=analysis,
        evidence=evidence
        if evidence is not None
        else {"reads": [{"tool": "ha_get_integration", "result": {"entry_id": _TARGET}}]},
    )
    assert plan is not None
    return plan


def test_executor_disabled_blocks_execution_before_ha_call(tmp_path: Path) -> None:
    async def run() -> None:
        _store, cases, executor, ha, _mcp = await build_stack(tmp_path, enabled=False)
        plan = await make_plan(cases)
        with pytest.raises(PermissionError, match="disabled"):
            await executor.approve_and_execute(plan["plan_id"])
        assert ha.reload_calls == []
        await executor.close()

    asyncio.run(run())


def test_ambiguous_or_missing_evidence_blocks_execution(tmp_path: Path) -> None:
    async def run() -> None:
        _store, cases, executor, ha, _mcp = await build_stack(tmp_path)
        ambiguous = await make_plan(
            cases,
            evidence={
                "reads": [
                    {"result": {"entry_id": _TARGET}},
                    {"result": {"entry_id": "entry_other999"}},
                ]
            },
        )
        with pytest.raises(PermissionError, match="unambiguous"):
            await executor.approve_and_execute(ambiguous["plan_id"])
        assert ha.reload_calls == []
        await executor.close()

    asyncio.run(run())


def test_risk_confidence_and_multiple_change_gates(tmp_path: Path) -> None:
    async def run() -> None:
        _store, cases, executor, ha, _mcp = await build_stack(tmp_path)
        medium = await make_plan(cases, risk="medium")
        with pytest.raises(PermissionError, match="low-risk"):
            await executor.approve_and_execute(medium["plan_id"])

        low_conf = await make_plan(cases, confidence=0.50)
        with pytest.raises(PermissionError, match="0.90"):
            await executor.approve_and_execute(low_conf["plan_id"])

        multi = await make_plan(
            cases,
            changes=[
                {"operation": "reload_config_entry", "target": _TARGET},
                {"operation": "reload_config_entry", "target": _TARGET},
            ],
        )
        with pytest.raises(PermissionError, match="exactly one"):
            await executor.approve_and_execute(multi["plan_id"])
        assert ha.reload_calls == []
        await executor.close()

    asyncio.run(run())


def test_successful_approved_reload_requires_post_verification_before_resolution(tmp_path: Path) -> None:
    async def run() -> None:
        _store, cases, executor, ha, mcp = await build_stack(tmp_path)
        executor.verification_seconds = 0
        plan = await make_plan(cases)
        result = await executor.approve_and_execute(plan["plan_id"])
        assert result["status"] == "verifying"
        assert ha.reload_calls == [_TARGET]
        await asyncio.sleep(0.05)

        stored = await executor.get_plan(plan["plan_id"])
        assert stored is not None
        assert stored["status"] == "succeeded"
        case = await cases.get_case(_PATTERN)
        assert case is not None
        assert case["status"] == "resolved"
        assert case["notification_id"] in ha.dismissed
        assert mcp.calls == [
            (
                "ha_get_integration",
                {"entry_id": _TARGET},
                "post-repair config-entry verification",
            )
        ]
        with sqlite3.connect(executor.db_path) as db:
            row = db.execute(
                "SELECT trust_class, source, outcome FROM knowledge WHERE memory_key=?",
                (f"repair:{plan['plan_id']}",),
            ).fetchone()
        assert row == ("verified_fix", "autodoctor-approved-repair", "verified")
        await executor.close()

    asyncio.run(run())


def test_recurrence_after_execution_fails_verification_and_keeps_case_open(tmp_path: Path) -> None:
    async def run() -> None:
        _store, cases, executor, ha, _mcp = await build_stack(tmp_path)
        executor.verification_seconds = 3600
        plan = await make_plan(cases)
        result = await executor.approve_and_execute(plan["plan_id"])
        assert result["status"] == "verifying"
        await cases.record_event(
            pattern_key=_PATTERN,
            pattern_label="integration reload candidate",
            family="test",
            fingerprint="fp-repair",
            event=LogEvent("ERROR", "test", "", "config entry failed again", "test.integration", cases._now() + 1),
            fingerprint_is_new=False,
        )
        await executor._verify_execution(result["execution_id"])
        stored = await executor.get_plan(plan["plan_id"])
        assert stored is not None
        assert stored["status"] == "failed"
        case = await cases.get_case(_PATTERN)
        assert case is not None
        assert case["status"] == "needs_user_action"
        assert case["notification_id"] not in ha.dismissed
        await executor.close()

    asyncio.run(run())


def test_ha_rejection_is_recorded_as_failed_not_success(tmp_path: Path) -> None:
    async def run() -> None:
        ha = FakeHA(fail_reload=True)
        _store, cases, executor, _ha, _mcp = await build_stack(tmp_path, ha=ha)
        plan = await make_plan(cases)
        with pytest.raises(RuntimeError, match="reload failed"):
            await executor.approve_and_execute(plan["plan_id"])
        stored = await executor.get_plan(plan["plan_id"])
        assert stored is not None
        assert stored["status"] == "failed"
        assert ha.reload_calls == [_TARGET]
        await executor.close()

    asyncio.run(run())
