from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from test_backup_first_repairs import ENTITY, TARGET, make_plan, recipe_plan, stack, verify
from autodoctor.integration_planner import IntegrationReloadPlanner
from autodoctor.proactive import ProactiveMonitor, observation_identity
from autodoctor.repair_backup import RepairBlocked


def monitor(settings, store, cases, ha, clock):
    planner = IntegrationReloadPlanner(settings, cases, ha)
    return ProactiveMonitor(settings, store, cases, ha, planner, clock=lambda: clock[0])


def test_sustained_entity_fault_is_observed_without_ha_mutations_and_recovers(tmp_path):
    async def run():
        async with stack(tmp_path, proactive_checks_enabled=True, proactive_entities=["sensor.test"],
                         integration_reload_repair_enabled=False) as (s, store, cases, ex, ha, backups, _, clock):
            state = {"entity_id": "sensor.test", "state": "unavailable"}
            async def get_state(_target):
                return dict(state)
            ha.get_state = get_state
            watch = monitor(s, store, cases, ha, clock)
            await watch.initialize()
            await watch.run_once()
            assert await cases.list_cases() == []
            clock[0] += 180
            await watch.run_once()
            key, _ = observation_identity("entity", "sensor.test")
            assert (await cases.get_case(key))["occurrences"] == 1
            # A new worker uses persistent observations; duplicate scans do not invent evidence.
            restarted = monitor(s, store, cases, ha, clock)
            await restarted.run_once()
            assert (await cases.get_case(key))["occurrences"] == 1
            state["state"] = "on"
            clock[0] += 60
            await restarted.run_once()
            assert (await cases.get_case(key))["status"] == "diagnosed"
            clock[0] += 60
            await restarted.run_once()
            assert (await cases.get_case(key))["status"] == "resolved"
            assert await cases.list_repair_plans() == []
            assert ha.reloads == []
            assert backups.events == []
    asyncio.run(run())


def test_only_enrolled_reporting_entities_are_checked_for_staleness(tmp_path):
    async def run():
        async with stack(tmp_path, proactive_checks_enabled=True, proactive_entities=["sensor.stable"],
                         proactive_stale_entities=["sensor.reporting"], integration_reload_repair_enabled=False) as (s, store, cases, _, ha, _, _, clock):
            async def get_state(target):
                stamp = datetime.fromtimestamp(clock[0] - 10000, timezone.utc).isoformat()
                return {"entity_id": target, "state": "1", "last_updated": stamp, "last_reported": stamp}
            ha.get_state = get_state
            watch = monitor(s, store, cases, ha, clock)
            await watch.initialize()
            await watch.run_once()
            clock[0] += 180
            await watch.run_once()
            rows = await cases.list_cases()
            assert len(rows) == 1
            assert rows[0]["pattern_key"] == observation_identity("entity", "sensor.reporting")[0]
            assert rows[0]["pattern_label"] == "stale_entity"
            assert "sensor.reporting" not in json.dumps(watch.health())
    asyncio.run(run())


def test_failed_read_is_neither_fault_evidence_nor_recovery(tmp_path):
    async def run():
        async with stack(tmp_path, proactive_checks_enabled=True, proactive_entities=["sensor.test"],
                         integration_reload_repair_enabled=False) as (s, store, cases, _, ha, _, _, clock):
            async def unavailable(_target):
                return {"entity_id": "sensor.test", "state": "unavailable"}
            ha.get_state = unavailable
            watch = monitor(s, store, cases, ha, clock)
            await watch.initialize()
            await watch.run_once()
            clock[0] += 180
            await watch.run_once()
            before = (await cases.list_cases())[0]
            async def failed(_target):
                raise TimeoutError("private endpoint")
            ha.get_state = failed
            clock[0] += 60
            await watch.run_once()
            after = (await cases.list_cases())[0]
            assert after["occurrences"] == before["occurrences"]
            assert after["status"] == before["status"]
            assert watch.health()["read_failures"] == 1
    asyncio.run(run())


def test_silent_integration_failure_creates_and_executes_one_backed_up_plan_without_ai(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, ex, ha, backups, _, clock):
            watch = monitor(s, store, cases, ha, clock)
            await watch.initialize()
            await watch.run_once()
            clock[0] += 180
            await watch.run_once()
            plans = await cases.list_repair_plans()
            assert len(plans) == 1
            assert plans[0]["evidence"]["origin"] == "compiled_integration_reload"
            result = await ex.auto_execute(plans[0]["plan_id"])
            assert ha.reloads == [TARGET]
            assert backups.events.count("create") == 1
            assert (await verify(ex, result, clock))["stage"] == "succeeded"
            await watch.run_once()
            clock[0] += 60
            await watch.run_once()
            assert len(await cases.list_repair_plans()) == 1
            assert (await store.monthly_ai_usage())["attempts_count"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("changed", [{"domain": "alarm_control_panel"}, {"disabled_by": "user"},
                                     {"entry_id": "different_entry"}, {"state": "setup_error"}, {"state": "loaded"}])
def test_automatic_reload_refuses_protected_disabled_ambiguous_or_unsupported_targets(tmp_path, changed):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, backups, _, clock):
            plan = await make_plan(store, cases, clock)
            async def entry(target):
                return {"entry_id": target, "domain": "tplink", "state": "setup_retry", "disabled_by": None, **changed}
            ha.get_config_entry_status = entry
            with pytest.raises(RepairBlocked):
                await ex.auto_execute(plan["plan_id"])
            assert ha.reloads == []
            assert backups.events == []
    asyncio.run(run())


def test_enrollment_is_rechecked_after_backup_before_any_reload(tmp_path):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, backups, _, clock):
            plan = await make_plan(store, cases, clock)
            backups.after_create = lambda: setattr(ex, "settings", replace(ex.settings, integration_reload_targets=[]))
            with pytest.raises(RepairBlocked, match="not_enrolled"):
                await ex.auto_execute(plan["plan_id"])
            assert ha.reloads == []
            assert (await ex.journal.backups())[0]["protected"] == 1
    asyncio.run(run())


def test_unenrolled_automatic_reload_is_blocked_while_manual_path_still_requires_backup(tmp_path):
    async def run():
        async with stack(tmp_path, integration_reload_targets=[]) as (_, store, cases, ex, ha, backups, _, clock):
            plan = await make_plan(store, cases, clock)
            with pytest.raises(RepairBlocked, match="not_enrolled"):
                await ex.auto_execute(plan["plan_id"])
            assert ha.reloads == []
            result = await ex.approve_and_execute(plan["plan_id"])
            assert backups.events.count("create") == 1
            assert (await verify(ex, result, clock))["stage"] == "succeeded"
    asyncio.run(run())


def test_diagnostic_late_natural_evidence_reconciles_without_repeating_the_patch(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, ex, _, backups, client, clock):
            plan = await recipe_plan(store, cases, s, client, clock)
            client.natural_run = False
            result = await ex.auto_execute(plan["plan_id"])
            assert (await verify(ex, result, clock))["stage"] == "verification_inconclusive"
            client.natural_run = True
            clock[0] += 60
            assert await ex._reconcile_inconclusive_audit_log_repairs() == 1
            assert len(client.writes) == 1
            assert backups.events.count("create") == 1
            assert (await cases.get_case(plan["pattern_key"]))["repair_plan_id"] == plan["plan_id"]
            assert await ex._reconcile_inconclusive_audit_log_repairs() == 0
    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_integration_verification_recovers_temporary_read_failure_but_retains_expired_hold(tmp_path, expired):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, backups, _, clock):
            plan = await make_plan(store, cases, clock)
            result = await ex.auto_execute(plan["plan_id"])
            original = ha.get_config_entry_status
            async def fail(_target):
                raise TimeoutError("temporary evidence failure")
            ha.get_config_entry_status = fail
            assert (await verify(ex, result, clock))["stage"] == "verification_inconclusive"
            ha.get_config_entry_status = original
            clock[0] += 90000 if expired else 60
            assert await ex._reconcile_inconclusive_audit_log_repairs() == (0 if expired else 1)
            attempt = await ex.journal.get(result["execution_id"])
            assert attempt["protected"] == int(expired)
            assert ha.reloads == [TARGET]
            assert backups.events.count("create") == 1
    asyncio.run(run())
