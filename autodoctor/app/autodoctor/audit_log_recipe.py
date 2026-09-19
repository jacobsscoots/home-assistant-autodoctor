"""Fixed JSON-to-Base64 repair for one explicitly reviewed append-only logger.

This compiles a configuration change; it never evaluates templates or runs a shell.
The operator's full-config digest binds the existing payload and terminal service.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .repair_backup import RepairBlocked
from .repair_journal import config_digest

REPAIR_TYPE = "script_json_base64"
RECIPE_ID = "script_json_base64_v1"
ORIGIN = "compiled_audit_log_recipe"
ENTITY = re.compile(r"script\.[a-z0-9_]+\Z")
KEY = re.compile(r"[a-z0-9_]+\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_JSON = re.compile(r"\s*{{\s*payload\s*\|\s*to_json\s*}}\s*\Z")
_B64 = re.compile(r"\s*{{\s*json_line\s*\|\s*base64_encode\s*}}\s*\Z")
_ARGUMENT = re.compile(r"\s*{{\s*b64\s*}}\s*\Z")
_SHELL = re.compile(r"shell_command\.[a-z0-9_]+\Z")
_FIELDS = {"category", "event", "entity", "old_state", "new_state", "automation", "trigger_id", "notes"}
_TOP = {"alias", "description", "icon", "mode", "max", "max_exceeded", "fields", "sequence", "trace"}
PATCHED_EXPRESSION = "{{ payload | to_json | base64_encode }}"


def matches_incident(text: str) -> bool:
    return "a bytes-like object is required, not 'wrapper'" in text.lower()


def enrolled(settings: Any) -> bool:
    return (
        settings.audit_log_repair_enabled is True
        and isinstance(settings.audit_log_repair_entity, str)
        and ENTITY.fullmatch(settings.audit_log_repair_entity) is not None
        and isinstance(settings.audit_log_repair_config_sha256, str)
        and DIGEST.fullmatch(settings.audit_log_repair_config_sha256) is not None
    )


def _variables(config: dict[str, Any]) -> dict[str, Any]:
    sequence = config.get("sequence")
    if not isinstance(sequence, list) or len(sequence) != 2:
        raise RepairBlocked("audit_log_sequence_not_supported")
    first, last = sequence
    if not isinstance(first, dict) or set(first) - {"variables", "alias"}:
        raise RepairBlocked("audit_log_variables_step_not_supported")
    variables = first.get("variables")
    if not isinstance(variables, dict) or list(variables) != ["payload", "json_line", "b64"]:
        raise RepairBlocked("audit_log_variable_order_or_shape_mismatch")
    _validate_terminal(last)
    if not isinstance(variables["payload"], (dict, str)):
        raise RepairBlocked("audit_log_payload_shape_mismatch")
    # Removing the intermediate variable must not remove a payload dependency.
    if re.search(r"\b(?:json_line|b64)\b", str(variables["payload"])):
        raise RepairBlocked("audit_log_payload_depends_on_encoding_variables")
    return variables


def _validate_terminal(last: Any) -> None:
    if not isinstance(last, dict) or set(last) - {"action", "service", "data", "alias"}:
        raise RepairBlocked("audit_log_terminal_action_not_supported")
    if "action" in last and "service" in last:
        raise RepairBlocked("audit_log_ambiguous_terminal_service")
    service = last.get("action", last.get("service"))
    data = last.get("data")
    if not isinstance(service, str) or not _SHELL.fullmatch(service):
        raise RepairBlocked("audit_log_terminal_must_be_reviewed_shell_logger")
    if not isinstance(data, dict) or set(data) != {"b64"}:
        raise RepairBlocked("audit_log_terminal_arguments_mismatch")
    if not isinstance(data["b64"], str) or not _ARGUMENT.fullmatch(data["b64"]):
        raise RepairBlocked("audit_log_terminal_encoding_argument_mismatch")


def compile_repair(config: dict[str, Any]) -> dict[str, Any]:
    """Preserve the payload, inputs, shell call and metadata; change encoding only."""
    if not isinstance(config, dict) or set(config) - _TOP:
        raise RepairBlocked("audit_log_config_shape_not_supported")
    if config.get("mode") != "parallel" or type(config.get("max")) is not int or config["max"] != 10:
        raise RepairBlocked("audit_log_execution_contract_mismatch")
    if not isinstance(config.get("fields"), dict) or set(config["fields"]) != _FIELDS:
        raise RepairBlocked("audit_log_input_contract_mismatch")
    variables = _variables(config)
    if not isinstance(variables["json_line"], str) or not _JSON.fullmatch(variables["json_line"]):
        raise RepairBlocked("audit_log_json_expression_mismatch")
    if not isinstance(variables["b64"], str) or not _B64.fullmatch(variables["b64"]):
        raise RepairBlocked("audit_log_base64_expression_mismatch")
    updated = copy.deepcopy(config)
    target = updated["sequence"][0]["variables"]
    del target["json_line"]
    target["b64"] = PATCHED_EXPRESSION
    return updated


def validate_plan(settings: Any, plan: dict[str, Any], enabled: bool) -> tuple[bool, str, str | None]:
    if not enabled or not enrolled(settings):
        return False, "audit_log_recipe_not_enrolled", None
    if plan.get("status") != "proposed" or plan.get("risk") != "low":
        return False, "audit_log_plan_not_eligible", None
    changes = plan.get("proposed_changes")
    if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict):
        return False, "audit_log_requires_one_compiled_change", None
    change = changes[0]
    if (change.get("operation") != REPAIR_TYPE or change.get("recipe_id") != RECIPE_ID
            or change.get("entity_id") != settings.audit_log_repair_entity
            or change.get("before_digest") != settings.audit_log_repair_config_sha256
            or (plan.get("evidence") or {}).get("origin") != ORIGIN):
        return False, "audit_log_plan_not_bound_to_reviewed_config", None
    target, digest = change.get("target"), change.get("after_digest")
    if not isinstance(target, str) or not KEY.fullmatch(target):
        return False, "audit_log_invalid_script_key", None
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        return False, "audit_log_missing_postcondition", None
    return True, "eligible", target


def reviewed_patch(settings: Any, current: dict[str, Any]) -> dict[str, Any]:
    if not enrolled(settings) or config_digest(current) != settings.audit_log_repair_config_sha256:
        raise RepairBlocked("audit_log_config_not_equal_to_operator_review")
    return compile_repair(current)
