from __future__ import annotations

import asyncio
import copy
import json
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.backup_executor import BackupFirstRepairExecutor
from autodoctor.cases import IncidentCaseManager
from autodoctor.config import Settings
from autodoctor.diagnostic_planner import DiagnosticRepairPlanner
from autodoctor.diagnostic_recipe import RECIPE_TYPE, compile_repair
from autodoctor.models import Analysis, LogEvent
from autodoctor.repair_backup import BackupUncertain, MIB, RepairBlocked, SupervisorBackupClient
from autodoctor.repair_journal import config_digest
from autodoctor.store import IncidentStore

TARGET = "entry_test123"
ENTITY = "automation.diagnostic_test"
CONFIG = {"id": "diagnostic_test_id", "alias": "Test diagnostic", "triggers": [{"trigger": "state", "entity_id": "sensor.example"}],
          "actions": [{"action": "system_log.write", "data": {"message": "Previous {{ trigger.from_state.state }}"}}]}


class FakeHA:
    session = None

    def __init__(self):
        self.state = "setup_error"
        self.reloads = []
        self.fail_reload = False

    async def get_config_entry_status(self, target):
        return {"entry_id": target, "state": self.state, "disabled_by": None}

    async def reload_config_entry(self, target):
        self.reloads.append(target)
        if self.fail_reload:
            raise RuntimeError("test failure")
        self.state = "loaded"

    async def notify(self, *args):
        pass

    async def dismiss_notification(self, *args):
        pass


class FakeBackups:
    validate_snapshot = staticmethod(SupervisorBackupClient.validate_snapshot)

    def __init__(self):
        self.items = {}
        self.sequence = 0
        self.events = []
        self.change_info = None
        self.after_create = None
        self.fail_create = False
        self.job_ok = True
        self.space_ok = True

    async def preflight(self, size):
        self.events.append("preflight")
        assert size >= 128 * MIB
        if not self.space_ok:
            raise RepairBlocked("insufficient_backup_space")

    async def create(self, *, name, password, marker):
        self.events.append("create")
        if self.fail_create:
            raise BackupUncertain("backup_creation_outcome_uncertain")
        self.sequence += 1
        slug = "backup_" + str(self.sequence)
        self.items[slug] = {"slug": slug, "type": "partial", "protected": True,
                            "homeassistant": "2026.9.3", "homeassistant_exclude_database": True,
                            "addons": [], "folders": [], "location": None, "size_bytes": MIB,
                            "extra": {"autodoctor": marker}}
        if self.change_info:
            self.items[slug].update(self.change_info)
        if self.after_create:
            self.after_create()
        return slug, "job_test"

    async def verify_job(self, job_id):
        self.events.append("verify_job")
        if not self.job_ok:
            raise BackupUncertain("backup_job_not_confirmed_complete")

    async def inspect(self, slug):
        self.events.append("inspect")
        return self.items[slug]

    async def delete_local(self, slug):
        self.events.append("delete:" + slug)
        del self.items[slug]


class FakeDiagnostic:
    def __init__(self):
        self.config = copy.deepcopy(CONFIG)
        self.natural_run = True
        self.writes = []

    async def resolve(self, entity):
        assert entity == ENTITY
        return self.config["id"], copy.deepcopy(self.config)

    async def read(self, config_id):
        return copy.deepcopy(self.config)

    async def write_checked(self, config_id, expected, updated):
        if config_digest(self.config) != config_digest(expected):
            raise RepairBlocked("diagnostic_config_changed_no_overwrite")
        self.writes.append(copy.deepcopy(updated))
        self.config = copy.deepcopy(updated)

    async def natural_run_verified(self, config_id, since, expected_config):
        return self.natural_run


@asynccontextmanager
async def stack(tmp_path, **overrides):
    settings = replace(Settings(repair_executor_enabled=True, auto_apply_low_risk=True,
                                repair_backup_password=secrets.token_urlsafe(24),
                                diagnostic_template_repair_enabled=True,
                                diagnostic_repair_entities=[ENTITY]), **overrides)
    path = str(tmp_path / "autodoctor.db")
    store = IncidentStore(path)
    await store.initialize()
    ha, backups, diagnostic = FakeHA(), FakeBackups(), FakeDiagnostic()
    cases = IncidentCaseManager(path, ha, notifications_enabled=False)
    await cases.initialize()
    executor = BackupFirstRepairExecutor(settings, path, ha, None, cases, backup_client=backups, diagnostic_client=diagnostic)
    await executor.initialize()
    clock = [time.time()]
    executor._now = lambda: clock[0]
    try:
        yield settings, store, cases, executor, ha, backups, diagnostic, clock
    finally:
        await executor.close()


async def make_plan(store, cases, clock, *, target=TARGET):
    pattern = "test/reload/" + target
    event = LogEvent("ERROR", "test.py", "", "config entry failed", "homeassistant.components.test", clock[0] - 10)
    row, new = await store.record(pattern, event, pattern, "other")
    await cases.record_event(pattern_key=pattern, pattern_label="other", family="test", fingerprint=pattern, event=event, fingerprint_is_new=new)
    return await cases.apply_analysis(pattern_key=pattern, fingerprint=pattern,
                                     analysis=Analysis("Reload test", "Test unhealthy entry", 0.97, "low", "propose_fix", [], [], [{"operation": "reload_config_entry", "target": target}]),
                                     evidence={"entry_id": target})


async def verify(executor, result, clock):
    await executor.close()  # Stop real-time sleeps; advance the deterministic test clock.
    clock[0] += executor.verification_seconds + 1
    await executor._verify_execution(result["execution_id"])
    return await executor.journal.get(result["execution_id"])


def test_backup_first_execution_and_real_verification_gate(tmp_path):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, backups, _, clock):
            plan = await make_plan(store, cases, clock)
            result = await ex.auto_execute(plan["plan_id"])
            assert ha.reloads == [TARGET]
            assert backups.events[:3] == ["preflight", "create", "verify_job"]
            row = await ex.journal.get(result["execution_id"])
            assert row["stage"] == "verifying"
            assert row["protected"] == 1
            await ex._verify_execution(result["execution_id"])
            assert (await ex.get_plan(plan["plan_id"]))["status"] == "verifying"
            final = await verify(ex, result, clock)
            assert final["stage"] == "succeeded"
            assert final["protected"] == 0
            assert len(await ex.journal.backups()) == 1
            assert (await cases.get_case(plan["pattern_key"]))["status"] == "resolved"
    asyncio.run(run())


@pytest.mark.parametrize("bad", [{"protected": False}, {"homeassistant": None}, {"homeassistant_exclude_database": False},
                                  {"addons": ["other"]}, {"folders": ["media"]}, {"extra": {}}, {"size_bytes": 0}])
def test_invalid_backups_never_allow_a_repair(tmp_path, bad):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, backups, _, clock):
            backups.change_info = bad
            plan = await make_plan(store, cases, clock)
            with pytest.raises(RepairBlocked):
                await ex.auto_execute(plan["plan_id"])
            assert ha.reloads == []
            assert not any(event.startswith("delete:") for event in backups.events)
            assert (await ex.journal.health())["uncertain_attempts"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["create", "job", "space", "password"])
def test_backup_setup_and_failures_block_mutation_without_retries(tmp_path, failure):
    async def run():
        args = {"repair_backup_password": ""} if failure == "password" else {}
        async with stack(tmp_path, **args) as (_, store, cases, ex, ha, backups, _, clock):
            backups.fail_create = failure == "create"
            backups.job_ok = failure != "job"
            backups.space_ok = failure != "space"
            plan = await make_plan(store, cases, clock)
            with pytest.raises(RepairBlocked):
                await ex.auto_execute(plan["plan_id"])
            assert ha.reloads == []
            assert backups.events.count("create") <= 1
    asyncio.run(run())


def test_target_revalidated_after_backup(tmp_path):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, backups, _, clock):
            backups.after_create = lambda: setattr(ha, "state", "loaded")
            plan = await make_plan(store, cases, clock)
            with pytest.raises(RepairBlocked, match="not_proven_unhealthy"):
                await ex.auto_execute(plan["plan_id"])
            assert ha.reloads == []
            assert (await ex.journal.backups())[0]["protected"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("keep", [1, 2])
def test_retention_keeps_only_owned_backups_per_stable_target(tmp_path, keep):
    async def run():
        async with stack(tmp_path, repair_backup_keep=keep) as (_, store, cases, ex, ha, backups, _, clock):
            backups.items["manual"] = {"name": "AutoDoctor pretend manual backup"}
            for _ in range(3):
                ha.state = "setup_error"
                plan = await make_plan(store, cases, clock)
                result = await ex.auto_execute(plan["plan_id"])
                await verify(ex, result, clock)
                clock[0] += 3700
            assert "manual" in backups.items
            assert len(await ex.journal.backups()) == keep
            assert len(backups.items) == keep + 1
    asyncio.run(run())


def test_failed_repair_pins_backup_and_blocks_target_retry(tmp_path):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, backups, _, clock):
            ha.fail_reload = True
            plan = await make_plan(store, cases, clock)
            with pytest.raises(RepairBlocked):
                await ex.auto_execute(plan["plan_id"])
            clock[0] += 4000
            second = await make_plan(store, cases, clock)
            with pytest.raises(RepairBlocked, match="uncertain_repair"):
                await ex.auto_execute(second["plan_id"])
            assert ha.reloads == [TARGET]
            assert backups.events.count("create") == 1
            assert (await ex.journal.backups())[0]["protected"] == 1
    asyncio.run(run())


def test_competing_executors_do_not_duplicate_backup_or_reload(tmp_path):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, first, ha, backups, diagnostic, clock):
            second = BackupFirstRepairExecutor(settings, first.db_path, ha, None, cases, backup_client=backups, diagnostic_client=diagnostic)
            await second.initialize()
            plan = await make_plan(store, cases, clock)
            try:
                results = await asyncio.gather(first.auto_execute(plan["plan_id"]), second.auto_execute(plan["plan_id"]), return_exceptions=True)
                assert sum(isinstance(item, dict) for item in results) == 1
                assert backups.events.count("create") == 1
                assert ha.reloads == [TARGET]
            finally:
                await second.close()
    asyncio.run(run())


def test_restart_resumes_verification_without_replaying_mutation(tmp_path):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, first, ha, backups, diagnostic, clock):
            plan = await make_plan(store, cases, clock)
            result = await first.auto_execute(plan["plan_id"])
            await first.close()
            second = BackupFirstRepairExecutor(settings, first.db_path, ha, None, cases, backup_client=backups, diagnostic_client=diagnostic)
            await second.initialize()
            second._now = lambda: clock[0]
            try:
                assert await second.resume_pending_verifications() == 1
                await verify(second, result, clock)
                assert ha.reloads == [TARGET]
                assert backups.events.count("create") == 1
            finally:
                await second.close()
    asyncio.run(run())


def test_interrupted_backup_protects_recovery_and_never_replays(tmp_path):
    async def run():
        async with stack(tmp_path) as (_, store, cases, ex, ha, backups, _, clock):
            entered = asyncio.Event()
            async def interrupted_create(**kwargs):
                entered.set()
                await asyncio.Event().wait()
            backups.create = interrupted_create
            plan = await make_plan(store, cases, clock)
            task = asyncio.create_task(ex.auto_execute(plan["plan_id"]))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await ex.resume_pending_verifications() == 0
            assert (await ex.journal.health())["uncertain_attempts"] == 1
            assert ha.reloads == []
            assert (await cases.get_case(plan["pattern_key"]))["status"] == "needs_user_action"
    asyncio.run(run())


async def recipe_plan(store, cases, settings, diagnostic, clock):
    planner = DiagnosticRepairPlanner(settings, cases, diagnostic)
    event = LogEvent("ERROR", "helpers/script.py", "", "UndefinedError: None has no attribute 'state'", "homeassistant.components." + ENTITY, clock[0] - 1)
    pattern = "automation/template_error/testrecipe"
    for _ in range(2):
        row, new = await store.record("recipe-fp", event, pattern, "template_error")
        await cases.record_event(pattern_key=pattern, pattern_label="template_error", family="automation", fingerprint="recipe-fp", event=event, fingerprint_is_new=new)
    assert await planner.consider(event, "recipe-fp", pattern, row)
    return (await cases.list_repair_plans())[0]


def test_recipe_creates_plan_without_ai_and_backups_before_patch(tmp_path):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, ex, _, backups, diagnostic, clock):
            plan = await recipe_plan(store, cases, settings, diagnostic, clock)
            assert plan["repair_type"] == RECIPE_TYPE
            result = await ex.auto_execute(plan["plan_id"])
            assert diagnostic.config == compile_repair(CONFIG)
            assert len(diagnostic.writes) == 1
            assert backups.events.count("create") == 1
            assert (await verify(ex, result, clock))["stage"] == "succeeded"
    asyncio.run(run())


def test_recipe_does_not_claim_success_without_natural_execution(tmp_path):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, ex, _, _, diagnostic, clock):
            diagnostic.natural_run = False
            plan = await recipe_plan(store, cases, settings, diagnostic, clock)
            result = await ex.auto_execute(plan["plan_id"])
            row = await verify(ex, result, clock)
            assert row["stage"] == "verification_inconclusive"
            assert row["protected"] == 1
            assert (await ex.get_plan(plan["plan_id"]))["status"] != "succeeded"
    asyncio.run(run())


def test_recipe_rolls_back_when_origin_reports_new_error_pattern(tmp_path):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, ex, _, _, diagnostic, clock):
            plan = await recipe_plan(store, cases, settings, diagnostic, clock)
            result = await ex.auto_execute(plan["plan_id"])
            event = LogEvent("ERROR", "test.py", "", "different regression", "homeassistant.components." + ENTITY, clock[0] + 1)
            await store.record("new-error", event, "new/pattern", "other")
            row = await verify(ex, result, clock)
            assert row["stage"] == "rolled_back"
            assert diagnostic.config == CONFIG
            assert row["protected"] == 1
    asyncio.run(run())


def test_recipe_never_rolls_back_over_a_detected_intervening_edit(tmp_path):
    async def run():
        async with stack(tmp_path) as (settings, store, cases, ex, _, _, diagnostic, clock):
            plan = await recipe_plan(store, cases, settings, diagnostic, clock)
            result = await ex.auto_execute(plan["plan_id"])
            diagnostic.config["description"] = "Operator changed this"
            expected = copy.deepcopy(diagnostic.config)
            row = await verify(ex, result, clock)
            assert row["stage"] == "conflict"
            assert diagnostic.config == expected
            assert len(diagnostic.writes) == 1
    asyncio.run(run())


def test_health_does_not_expose_configuration_or_password(tmp_path):
    async def run():
        async with stack(tmp_path) as (settings, _, _, ex, _, _, _, _):
            health = json.dumps(await ex.health())
            assert settings.repair_backup_password not in health
            assert "preimage_json" not in health
            assert "postimage_json" not in health
            assert settings.repair_backup_password not in repr(settings)
    asyncio.run(run())
