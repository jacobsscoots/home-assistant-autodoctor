from __future__ import annotations

import asyncio
import copy

import pytest

from test_backup_first_repairs import CONFIG, make_plan, recipe_plan, stack, verify
from autodoctor.database import database_connection
from autodoctor.repair_backup import RepairBlocked


def test_notification_failure_does_not_abort_acknowledged_repair(tmp_path, monkeypatch):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, ex, _, _, diagnostic, clock):
            plan = await recipe_plan(store, cases, settings, diagnostic, clock)

            async def notification_failed(*args, **kwargs):
                raise RuntimeError("test notification unavailable")

            monkeypatch.setattr(cases, "publish_case", notification_failed)
            result = await ex.auto_execute(plan["plan_id"])
            assert result["status"] == "verifying"
            assert len(diagnostic.writes) == 1
            final = await verify(ex, result, clock)
            assert final["stage"] == "succeeded"
            assert len(diagnostic.writes) == 1
    asyncio.run(run())


@pytest.mark.parametrize("applied", [False, True])
def test_uncertain_diagnostic_save_is_not_replayed_or_rolled_back(tmp_path, applied):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, ex, _, _, diagnostic, clock):
            plan = await recipe_plan(store, cases, settings, diagnostic, clock)
            calls = []

            async def response_lost(config_id, expected, updated):
                calls.append(copy.deepcopy(updated))
                if applied:
                    diagnostic.config = copy.deepcopy(updated)
                raise TimeoutError("request may still be running")

            diagnostic.write_checked = response_lost
            with pytest.raises(RepairBlocked):
                await ex.auto_execute(plan["plan_id"])
            assert len(calls) == 1
            rows = await ex.journal.backups()
            assert rows[0]["stage"] == "mutation_uncertain"
            assert rows[0]["uncertain"] == 1
            assert rows[0]["protected"] == 1
    asyncio.run(run())


def test_verification_transition_failure_cannot_leave_half_committed_state(tmp_path):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, _, _, clock):
            plan = await make_plan(store, cases, clock)
            with database_connection(ex.db_path) as db:
                db.executescript("""
                    CREATE TRIGGER reject_verification_transition
                    BEFORE UPDATE OF status ON repair_executions
                    WHEN NEW.status='verifying'
                    BEGIN SELECT RAISE(ABORT, 'test verification write failure'); END;
                """)
            with pytest.raises(RepairBlocked):
                await ex.auto_execute(plan["plan_id"])
            row = (await ex.journal.backups())[0]
            assert row["stage"] == "mutation_uncertain"
            assert row["verification_started_at"] is None
            assert row["uncertain"] == 1
            assert len(ha.reloads) == 1
    asyncio.run(run())


def test_uncertain_rollback_creates_global_hold_and_preserves_snapshot(tmp_path):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, ex, _, _, diagnostic, clock):
            plan = await recipe_plan(store, cases, settings, diagnostic, clock)
            result = await ex.auto_execute(plan["plan_id"])
            await ex.close()
            calls = []

            async def rollback_response_lost(config_id, expected, updated):
                calls.append(copy.deepcopy(updated))
                raise TimeoutError("rollback may still be running")

            diagnostic.write_checked = rollback_response_lost
            row = await ex.journal.get(result["execution_id"])
            await ex._abort(plan, row, "test_demonstrated_regression")
            final = await ex.journal.get(result["execution_id"])
            assert final["stage"] == "mutation_uncertain"
            assert final["uncertain"] == 1
            assert final["protected"] == 1
            assert calls == [CONFIG]
    asyncio.run(run())
