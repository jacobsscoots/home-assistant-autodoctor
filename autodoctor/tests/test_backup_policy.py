from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.diagnostic_recipe import compile_repair, enrolled_origin, matches_incident
from autodoctor.repair_backup import RepairBlocked, job_succeeded, positive_size, valid_id


def diagnostic(message="{{ trigger.from_state.state }} → {{ trigger.to_state.state }}"):
    return {"id": "diagnostic_test", "triggers": [{"trigger": "state", "entity_id": "sensor.example"}],
            "conditions": [{"condition": "template", "value_template": "{{ true }}"}],
            "actions": [{"action": "system_log.write", "data": {"message": message}}]}


def test_compiled_recipe_changes_only_message_and_preserves_normal_state_expression():
    before = diagnostic()
    original = copy.deepcopy(before)
    after = compile_repair(before)
    assert before == original
    assert after["triggers"] == before["triggers"]
    assert after["conditions"] == before["conditions"]
    assert "trigger.from_state is not none" in after["actions"][0]["data"]["message"]
    assert "trigger.to_state is not none" in after["actions"][0]["data"]["message"]
    with pytest.raises(RepairBlocked, match="no_supported_template_expression"):
        compile_repair(after)


@pytest.mark.parametrize("service", ["light.turn_on", "switch.turn_off", "automation.trigger", "script.audit_log", "climate.set_temperature", "lock.unlock"])
def test_recipe_refuses_control_or_indirect_actions(service):
    config = diagnostic()
    config["actions"].append({"action": service, "data": {"message": "test"}})
    with pytest.raises(RepairBlocked, match="not_strictly_log_only"):
        compile_repair(config)


@pytest.mark.parametrize("extra", [{"choose": []}, {"response_variable": "x"}, {"variables": {"x": 1}}, {"target": {"entity_id": "light.test"}}])
def test_recipe_refuses_unrecognised_action_fields(extra):
    config = diagnostic()
    config["actions"][0].update(extra)
    with pytest.raises(RepairBlocked):
        compile_repair(config)


def test_missing_and_ambiguous_origins_cannot_be_enrolled_by_alias():
    assert enrolled_origin("homeassistant.helpers.script", ["automation.test"]) is None
    assert enrolled_origin("homeassistant.components.automation.test", []) is None
    assert enrolled_origin("homeassistant.components.automation.test", ["automation.test"]) == "automation.test"
    assert not matches_incident("network unavailable")


@pytest.mark.parametrize("value", ["../backup", "a/b", "x?y", "", None])
def test_backup_identifiers_cannot_escape_fixed_endpoint(value):
    with pytest.raises(RepairBlocked):
        valid_id(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, True, "10"])
def test_invalid_backup_sizes_fail_closed(value):
    with pytest.raises(RepairBlocked):
        positive_size(value)


def test_completed_backup_job_requires_all_children_and_no_errors():
    child = {"done": True, "errors": [], "child_jobs": []}
    assert job_succeeded({"done": True, "child_jobs": [child]})
    assert not job_succeeded({"done": False, "child_jobs": []})
    assert not job_succeeded({"done": True, "child_jobs": [{"done": False}]})
    assert not job_succeeded({"done": True, "errors": ["failed"]})


def test_production_entrypoint_has_no_unbacked_executor_path():
    source = (ROOT / "main.py").read_text()
    assert "executor = BackupFirstRepairExecutor(" in source
    assert "executor = AutoApplyRepairExecutor(" not in source
    assert "DiagnosticRepairPlanner(" in source
    assert "os.umask(0o077)" in source
