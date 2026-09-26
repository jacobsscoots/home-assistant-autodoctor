from __future__ import annotations

import asyncio
import base64
import copy
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from test_audit_log_recipe import ENTITY, SCRIPT_KEY, logger_config
from test_backup_first_repairs import stack, verify
from autodoctor.audit_log_client import AuditLogHAClient
from autodoctor.audit_log_planner import AuditLogRepairPlanner
from autodoctor.audit_log_recipe import REPAIR_TYPE, compile_repair
from autodoctor.models import LogEvent
from autodoctor.repair_backup import RepairBlocked
from autodoctor.repair_journal import config_digest


class FakeAudit:
    def __init__(self):
        self.config = logger_config()
        self.writes = []
        self.busy = False
        self.proof = True
        self.key = SCRIPT_KEY

    async def resolve(self):
        if self.busy:
            raise RepairBlocked("audit_log_busy_or_run_count_unknown")
        return self.key, copy.deepcopy(self.config)

    async def read(self, key):
        assert key == self.key
        return copy.deepcopy(self.config)

    async def write_checked(self, key, expected, updated):
        if self.busy or config_digest(self.config) != config_digest(expected):
            raise RepairBlocked("audit_log_config_changed_no_overwrite")
        self.writes.append(copy.deepcopy(updated))
        self.config = copy.deepcopy(updated)

    async def natural_run_verified(self, key, since, expected):
        return self.proof


def settings_options():
    return {"audit_log_repair_enabled": True, "audit_log_repair_entity": ENTITY,
            "audit_log_repair_config_sha256": config_digest(logger_config())}


async def plan_event(planner, store, cases, timestamp, *, logger=None):
    event = LogEvent("ERROR", "helpers/script.py", "", "TypeError: a bytes-like object is required, not 'Wrapper'",
                     logger or "homeassistant.components.script." + SCRIPT_KEY, timestamp)
    fp, pattern = "synthetic-wrapper-fp", "script/template_error/synthetic"
    row, new = await store.record(fp, event, pattern, "template_error")
    await cases.record_event(pattern_key=pattern, pattern_label="template_error", family="script", fingerprint=fp, event=event, fingerprint_is_new=new)
    return await planner.consider(event, fp, pattern, row)


async def create_plan(settings, store, cases, executor, clock):
    executor.audit_logs = client = FakeAudit()
    planner = AuditLogRepairPlanner(settings, cases, client)
    assert await plan_event(planner, store, cases, clock[0] - 2)
    assert not await cases.list_repair_plans()
    assert await plan_event(planner, store, cases, clock[0] - 1)
    plan = (await cases.list_repair_plans())[0]
    assert plan["repair_type"] == REPAIR_TYPE
    return client, planner, plan


def test_live_events_create_backed_up_script_repair_without_ai(tmp_path):
    async def run():
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, ex, ha, backups, _, clock):
            client, _, plan = await create_plan(settings, store, cases, ex, clock)
            result = await ex.auto_execute(plan["plan_id"])
            assert client.config == compile_repair(logger_config())
            assert len(client.writes) == 1
            assert ha.reloads == []
            assert backups.events.count("create") == 1
            final = await verify(ex, result, clock)
            assert final["stage"] == "succeeded"
            assert final["protected"] == 0
            with sqlite3.connect(ex.db_path) as db:
                text = db.execute("SELECT resolution,verification,metadata_json FROM knowledge WHERE memory_key=?", ("repair:" + plan["plan_id"],)).fetchone()
            assert "config-entry reload" not in str(text)
            assert '"log_append_verified": false' in text[2]
    asyncio.run(run())


@pytest.mark.parametrize("change", [{"audit_log_repair_enabled": False}, {"audit_log_repair_config_sha256": ""},
                                    {"audit_log_repair_entity": "automation.not_a_script"}])
def test_unenrolled_script_never_gets_plan(tmp_path, change):
    async def run():
        options = {**settings_options(), **change}
        async with stack(tmp_path, **options) as (settings, store, cases, _, _, _, _, clock):
            planner = AuditLogRepairPlanner(settings, cases, FakeAudit())
            assert not await plan_event(planner, store, cases, clock[0] - 1)
            assert not await cases.list_repair_plans()
    asyncio.run(run())


def test_other_scripts_callers_and_repeated_delivery_cannot_qualify(tmp_path):
    async def run():
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, _, _, _, _, clock):
            planner = AuditLogRepairPlanner(settings, cases, FakeAudit())
            assert not await plan_event(planner, store, cases, clock[0] - 3, logger="homeassistant.components.script.other")
            assert not await plan_event(planner, store, cases, clock[0] - 2, logger="homeassistant.components.automation.caller")
            for _ in range(2):
                assert await plan_event(planner, store, cases, clock[0] - 1)
            assert not await cases.list_repair_plans()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["password", "backup", "job", "changed", "busy", "identity"])
def test_script_mutation_requires_every_backup_and_live_precondition(tmp_path, fault):
    async def run():
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, ex, _, backups, _, clock):
            client, _, plan = await create_plan(settings, store, cases, ex, clock)
            if fault == "password":
                from dataclasses import replace
                ex.settings = replace(settings, repair_backup_password="")
            if fault == "backup":
                backups.fail_create = True
            if fault == "job":
                backups.job_ok = False
            if fault == "changed":
                backups.after_create = lambda: client.config.update(description="Concurrent edit")
            if fault == "busy":
                client.busy = True
            if fault == "identity":
                client.key = "different_key"
            with pytest.raises(RepairBlocked):
                await ex.auto_execute(plan["plan_id"])
            assert not client.writes
    asyncio.run(run())


def test_wait_for_formerly_failing_natural_input_then_verify(tmp_path):
    async def run():
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, ex, _, _, _, clock):
            client, _, plan = await create_plan(settings, store, cases, ex, clock)
            client.proof = False
            result = await ex.auto_execute(plan["plan_id"])
            waiting = await verify(ex, result, clock)
            assert waiting["stage"] == "verifying"
            assert waiting["protected"] == 1
            client.proof = True
            clock[0] += 60
            await ex._verify_execution(result["execution_id"])
            assert (await ex.journal.get(result["execution_id"]))["stage"] == "succeeded"
    asyncio.run(run())


def test_natural_run_wait_is_bounded_without_claiming_success(tmp_path):
    async def run():
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, ex, _, _, _, clock):
            client, _, plan = await create_plan(settings, store, cases, ex, clock)
            client.proof = False
            result = await ex.auto_execute(plan["plan_id"])
            await ex.close()
            clock[0] += 901
            await ex._verify_execution(result["execution_id"])
            final = await ex.journal.get(result["execution_id"])
            assert final["stage"] == "verification_inconclusive"
            assert final["protected"] == 1
            assert len(client.writes) == 1
    asyncio.run(run())


@pytest.mark.parametrize("conflict", [False, True])
def test_new_script_error_rolls_back_only_unchanged_postimage(tmp_path, conflict):
    async def run():
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, ex, _, _, _, clock):
            client, _, plan = await create_plan(settings, store, cases, ex, clock)
            result = await ex.auto_execute(plan["plan_id"])
            if conflict:
                client.config["description"] = "Concurrent edit"
            event = LogEvent("ERROR", "script.py", "", "another regression", "homeassistant.components.script." + SCRIPT_KEY, clock[0] + 1)
            await store.record("new-fp", event, "new-script-pattern", "other")
            final = await verify(ex, result, clock)
            assert final["stage"] == ("conflict" if conflict else "rolled_back")
            assert len(client.writes) == (1 if conflict else 2)
            assert final["protected"] == 1
    asyncio.run(run())


def test_script_verification_resumes_after_restart_without_another_save(tmp_path):
    async def run():
        from autodoctor.backup_executor import BackupFirstRepairExecutor
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, ex, ha, backups, _, clock):
            client, _, plan = await create_plan(settings, store, cases, ex, clock)
            result = await ex.auto_execute(plan["plan_id"])
            await ex.close()
            resumed = BackupFirstRepairExecutor(settings, ex.db_path, ha, None, cases, backup_client=backups, audit_log_client=client)
            await resumed.initialize()
            resumed._now = lambda: clock[0]
            try:
                assert await resumed.resume_pending_verifications() == 1
                final = await verify(resumed, result, clock)
                assert final["stage"] == "succeeded"
                assert len(client.writes) == 1
                assert backups.events.count("create") == 1
            finally:
                await resumed.close()
    asyncio.run(run())


def trace_fixture(payload=None):
    payload = {"old": "off", "new": "on", "notes": "café"} if payload is None else payload
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    config = compile_repair(logger_config())
    trace = {"domain": "script", "item_id": SCRIPT_KEY, "run_id": "run_1", "state": "stopped", "script_execution": "finished",
             "timestamp": {"start": datetime.now(timezone.utc).isoformat()}, "config": config,
             "trace": {"sequence/0": [{"changed_variables": {}}], "sequence/1": [{"changed_variables": {"payload": payload, "b64": encoded},
                        "result": {"params": {"domain": "shell_command", "service": "synthetic_append", "service_data": {"b64": encoded}}}}]}}
    return trace, config


def test_natural_trace_proves_previously_failing_encoding_not_disk_append():
    trace, config = trace_fixture()
    assert AuditLogHAClient._verified_detail(trace, SCRIPT_KEY, 0, config)


@pytest.mark.parametrize("mutate", [
    lambda t: t.update(error="Template error"),
    lambda t: t.update(item_id="another"),
    lambda t: t.update(script_execution="aborted"),
    lambda t: t.update(config=logger_config()),
    lambda t: t["trace"].pop("sequence/1"),
    lambda t: t["trace"]["sequence/1"][0]["result"]["params"].update(service="other"),
    lambda t: t["trace"]["sequence/1"][0]["result"]["params"]["service_data"].update(b64="invalid!"),
    lambda t: t["trace"]["sequence/1"][0]["changed_variables"].update(payload={"different": "value"}),
])
def test_trace_proof_refuses_insufficient_or_wrong_evidence(mutate):
    trace, config = trace_fixture()
    mutate(trace)
    assert not AuditLogHAClient._verified_detail(trace, SCRIPT_KEY, 0, config)


@pytest.mark.parametrize("payload", [{"old": None}, {"old": False}, {"nested": {"bool": True}}])
def test_previously_successful_shapes_do_not_prove_former_failure_fixed(payload):
    trace, config = trace_fixture(payload)
    assert not AuditLogHAClient._verified_detail(trace, SCRIPT_KEY, 0, config)


def test_late_ai_observation_cannot_erase_compiled_proposal(tmp_path):
    async def run():
        from autodoctor.models import Analysis
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, ex, _, _, _, clock):
            _, _, plan = await create_plan(settings, store, cases, ex, clock)
            await cases.apply_analysis(pattern_key=plan["pattern_key"], fingerprint=plan["fingerprint"],
                                       analysis=Analysis("Observe", "Old AI result", 0.5, "low", "observe"))
            case = await cases.get_case(plan["pattern_key"])
            assert case["status"] == "repair_available"
            assert (await cases.list_repair_plans())[0]["plan_id"] == plan["plan_id"]
    asyncio.run(run())


def test_compiled_planner_shares_analysis_claim(tmp_path):
    async def run():
        from autodoctor.case_engine import CaseAwareAutoDoctorEngine
        from autodoctor.llm import NoProvider
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, _, ha, _, _, clock):
            engine = CaseAwareAutoDoctorEngine(settings, store, ha, NoProvider(), None)
            engine.cases = cases
            calls = []
            class Planner:
                async def consider(self, *args):
                    calls.append(True)
                    return True
            engine.audit_log_planner = Planner()
            assert await engine._claim_pattern("pattern")
            assert await engine._consider_compiled_repairs(None, "fp", "pattern", {})
            assert calls == []
            await engine._release_pattern("pattern")
            assert await engine._consider_compiled_repairs(None, "fp", "pattern", {})
            assert calls == [True]
            assert "pattern" not in engine._patterns_in_analysis
    asyncio.run(run())


def test_natural_verification_prefers_newest_traces_not_oldest():
    class TraceHA:
        def __init__(self):
            self.reads = []
            self.traces = []
            self.details = {}

        async def _repair_read(self, message):
            self.reads.append(message)
            if message["type"] == "trace/list":
                return self.traces
            return self.details[message["run_id"]]

    async def run():
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        ha = TraceHA()
        client = AuditLogHAClient(ha, type("Settings", (), {})())
        config = compile_repair(logger_config())
        for idx in range(25):
            ha.traces.append({
                "domain": "script", "item_id": SCRIPT_KEY, "run_id": f"old-{idx}",
                "state": "stopped", "script_execution": "finished",
                "timestamp": {"start": (now - timedelta(hours=1, minutes=idx)).isoformat()},
            })
        fresh, _ = trace_fixture()
        fresh["run_id"] = "fresh"
        fresh["timestamp"]["start"] = now.isoformat()
        ha.traces.append({
            "domain": "script", "item_id": SCRIPT_KEY, "run_id": "fresh",
            "state": "stopped", "script_execution": "finished",
            "timestamp": {"start": now.isoformat()},
        })
        ha.details["fresh"] = fresh
        assert await client.natural_run_verified(
            SCRIPT_KEY, (now - timedelta(minutes=5)).timestamp(), config
        )
        assert any(read.get("run_id") == "fresh" for read in ha.reads)

    asyncio.run(run())


def test_inconclusive_audit_repair_can_reconcile_from_later_natural_evidence(tmp_path):
    async def run():
        async with stack(tmp_path, **settings_options()) as (settings, store, cases, ex, _, _, _, clock):
            client, _, plan = await create_plan(settings, store, cases, ex, clock)
            client.proof = False
            result = await ex.auto_execute(plan["plan_id"])
            await ex.close()
            clock[0] += 901
            await ex._verify_execution(result["execution_id"])
            held = await ex.journal.get(result["execution_id"])
            assert held["stage"] == "verification_inconclusive"
            assert held["protected"] == 1

            client.proof = True
            assert await ex._reconcile_inconclusive_audit_log_repairs() == 1
            final = await ex.journal.get(result["execution_id"])
            assert final["stage"] == "succeeded"
            assert final["protected"] == 0
            with sqlite3.connect(ex.db_path) as db:
                assert db.execute(
                    "SELECT status FROM repair_executions WHERE execution_id=?",
                    (result["execution_id"],),
                ).fetchone()[0] == "succeeded"

    asyncio.run(run())
