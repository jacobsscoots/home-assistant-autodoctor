from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.automatic_repair import AutomaticRepairCoordinator


class FakeCases:
    def __init__(self, plans):
        self.plans = plans

    async def list_repair_plans(self, _limit):
        return list(self.plans)


class FakeExecutor:
    def __init__(self, *, enabled=True, allowed=True, pending=0):
        self.enabled = enabled
        self.allowed = allowed
        self.pending = pending
        self.calls = []

    async def health(self):
        return {"pending_verifications": self.pending}

    def validate_plan(self, plan):
        if self.allowed:
            return True, "approved", "entry_abc123"
        return False, "blocked by existing executor gate", None

    async def approve_and_execute(self, plan_id):
        self.calls.append(plan_id)
        return {"plan_id": plan_id, "execution_id": "exec_test", "status": "verifying"}


def _plan(created_at, plan_id="plan_test"):
    return {
        "plan_id": plan_id,
        "created_at": created_at,
        "status": "proposed",
        "risk": "low",
        "confidence": 0.97,
    }


def test_auto_apply_is_opt_in() -> None:
    async def run():
        executor = FakeExecutor()
        coordinator = AutomaticRepairCoordinator(
            SimpleNamespace(auto_apply_low_risk=False),
            FakeCases([_plan(10**12)]),
            executor,
        )
        assert await coordinator.run_once() == 0
        assert executor.calls == []

    asyncio.run(run())


def test_old_pending_plan_is_not_auto_applied() -> None:
    async def run():
        executor = FakeExecutor()
        coordinator = AutomaticRepairCoordinator(
            SimpleNamespace(auto_apply_low_risk=True),
            FakeCases([]),
            executor,
        )
        coordinator.cases = FakeCases([_plan(coordinator.started_at - 1)])
        assert await coordinator.run_once() == 0
        assert executor.calls == []

    asyncio.run(run())


def test_existing_executor_gate_must_pass() -> None:
    async def run():
        executor = FakeExecutor(allowed=False)
        coordinator = AutomaticRepairCoordinator(
            SimpleNamespace(auto_apply_low_risk=True),
            FakeCases([]),
            executor,
        )
        coordinator.cases = FakeCases([_plan(coordinator.started_at + 1)])
        assert await coordinator.run_once() == 0
        assert executor.calls == []

    asyncio.run(run())


def test_pending_verification_blocks_second_automatic_repair() -> None:
    async def run():
        executor = FakeExecutor(pending=1)
        coordinator = AutomaticRepairCoordinator(
            SimpleNamespace(auto_apply_low_risk=True),
            FakeCases([]),
            executor,
        )
        coordinator.cases = FakeCases([_plan(coordinator.started_at + 1)])
        assert await coordinator.run_once() == 0
        assert executor.calls == []

    asyncio.run(run())


def test_new_eligible_plan_uses_same_executor_path() -> None:
    async def run():
        executor = FakeExecutor()
        coordinator = AutomaticRepairCoordinator(
            SimpleNamespace(auto_apply_low_risk=True),
            FakeCases([]),
            executor,
        )
        coordinator.cases = FakeCases([_plan(coordinator.started_at + 1)])
        assert await coordinator.run_once() == 1
        assert executor.calls == ["plan_test"]
        assert await coordinator.run_once() == 0

    asyncio.run(run())
