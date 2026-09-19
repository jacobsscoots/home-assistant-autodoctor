"""One compiled recipe: null-safe trigger-state interpolation in log-only automations.

No general YAML editing, model-authored patches, physical actions or script calls.
"""
from __future__ import annotations

import copy
from datetime import datetime
import re
from typing import Any

from .repair_backup import RepairBlocked, valid_id
from .repair_journal import config_digest

RECIPE_TYPE = "diagnostic_log_template"
RECIPE_ID = "diagnostic_trigger_state_v1"
_ENTITY = re.compile(r"automation\.[a-z0-9_]+\Z")
_EXPR = re.compile(r"{{\s*trigger\.(from_state|to_state)\.state\s*}}")
_ALLOWED_SERVICES = {"system_log.write", "logbook.log"}
_ERROR_MARKERS = ("has no attribute 'state'", 'has no attribute "state"',
                  "has no attribute 'from_state'", "has no attribute 'to_state'")


def enrolled_origin(logger: str, enrolled: tuple[str, ...] | list[str]) -> str | None:
    prefix = "homeassistant.components."
    entity = logger.removeprefix(prefix)
    if not logger.startswith(prefix) or not _ENTITY.fullmatch(entity):
        return None
    return entity if entity in enrolled else None


def matches_incident(text: str) -> bool:
    return any(marker in text.lower() for marker in _ERROR_MARKERS)


def _safe_expression(match: re.Match[str]) -> str:
    field = match.group(1)
    return ("{{ trigger." + field + ".state if trigger is defined and trigger."
            + field + " is defined and trigger." + field + " is not none else 'unknown' }}")


def compile_repair(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with only direct trigger-state log interpolations guarded."""
    if not isinstance(config, dict) or not config.get("id"):
        raise RepairBlocked("diagnostic_config_missing_id")
    valid_id(config["id"])
    if "actions" in config and "action" in config:
        raise RepairBlocked("ambiguous_diagnostic_action_schema")
    action_key = "actions" if "actions" in config else "action"
    actions = config.get(action_key)
    if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
        raise RepairBlocked("diagnostic_actions_not_supported")
    updated = copy.deepcopy(config)
    replacements = 0
    for action in updated[action_key]:
        replacements += _patch_log_action(action)
    if not replacements:
        raise RepairBlocked("no_supported_template_expression")
    return updated


def _patch_log_action(action: Any) -> int:
    if not isinstance(action, dict) or set(action) - {"action", "service", "data", "alias"}:
        raise RepairBlocked("automation_is_not_strictly_log_only")
    if "action" in action and "service" in action:
        raise RepairBlocked("ambiguous_diagnostic_service")
    service = action.get("action", action.get("service"))
    if service not in _ALLOWED_SERVICES:
        raise RepairBlocked("automation_is_not_strictly_log_only")
    data = action.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("message"), str):
        raise RepairBlocked("diagnostic_message_not_supported")
    message = data["message"]
    if len(message) > 12000:
        raise RepairBlocked("diagnostic_message_too_large")
    data["message"], count = _EXPR.subn(_safe_expression, message)
    return count


class DiagnosticHAClient:
    """Fixed native HA config endpoints, outside the read-only diagnostic MCP.

    Native config POST validates before saving and reloads the selected automation.
    HA's API has no If-Match contract: last-moment readback is optimistic, not atomic
    across an independent UI/file editor. Enrolment requires no concurrent editing.
    """

    def __init__(self, ha: Any) -> None:
        self.ha = ha

    async def resolve(self, entity: str) -> tuple[str, dict[str, Any]]:
        if not _ENTITY.fullmatch(entity):
            raise RepairBlocked("invalid_diagnostic_entity")
        version = await self.ha.get_version()
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2026, 9, 3):
            raise RepairBlocked("diagnostic_recipe_requires_core_2026_9_3_or_newer")
        state = await self.ha.get_state(entity)
        if not isinstance(state, dict) or state.get("state") != "on":
            raise RepairBlocked("diagnostic_automation_not_enabled")
        config_id = valid_id((state.get("attributes") or {}).get("id"))
        config = await self.read(config_id)
        if config.get("id") != config_id:
            raise RepairBlocked("diagnostic_identity_mismatch")
        return config_id, config

    async def read(self, config_id: str) -> dict[str, Any]:
        url = f"{self.ha.api_base}/config/automation/config/{valid_id(config_id)}"
        async with self.ha.session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise RepairBlocked("live_diagnostic_config_unavailable")
            data = await response.json()
        if not isinstance(data, dict):
            raise RepairBlocked("live_diagnostic_config_invalid")
        return data

    async def write_checked(self, config_id: str, expected: dict[str, Any], updated: dict[str, Any]) -> None:
        current = await self.read(config_id)
        if config_digest(current) != config_digest(expected):
            raise RepairBlocked("diagnostic_config_changed_no_overwrite")
        if current.get("id") != config_id or updated.get("id") != config_id:
            raise RepairBlocked("diagnostic_identity_mismatch")
        url = f"{self.ha.api_base}/config/automation/config/{valid_id(config_id)}"
        async with self.ha.session.post(url, json=updated, allow_redirects=False) as response:
            if response.status != 200:
                raise RepairBlocked("diagnostic_save_rejected_or_uncertain")
            result = await response.json()
        if not isinstance(result, dict) or result.get("result") != "ok":
            raise RepairBlocked("diagnostic_save_outcome_uncertain")
        if config_digest(await self.read(config_id)) != config_digest(updated):
            raise RepairBlocked("diagnostic_postwrite_readback_mismatch")

    @staticmethod
    def _completed_trace(trace: dict[str, Any], since: float) -> bool:
        timestamp = trace.get("timestamp")
        if not isinstance(timestamp, dict):
            return False
        try:
            started_at = datetime.fromisoformat(timestamp["start"])
        except (KeyError, ValueError, TypeError):
            return False
        return (
            started_at.tzinfo is not None and started_at.timestamp() >= since
            and not trace.get("error") and trace.get("state") == "stopped"
            and trace.get("script_execution") == "finished"
        )

    async def natural_run_verified(
        self, config_id: str, since: float, expected_config: dict[str, Any],
    ) -> bool:
        # A summary alone could describe a run before the selected reload completed.
        # Require trace/get evidence that the completed run used the patched config.
        traces = await self.ha.read_automation_traces(config_id)
        for trace in traces:
            if not self._completed_trace(trace, since):
                continue
            run_id = trace.get("run_id")
            if not isinstance(run_id, str):
                continue
            detail = await self.ha.read_automation_trace(config_id, run_id)
            if not self._completed_trace(detail, since):
                continue
            if detail.get("domain") != "automation" or detail.get("item_id") != config_id:
                continue
            config = detail.get("config")
            if isinstance(config, dict) and config_digest(config) == config_digest(expected_config):
                return True
        return False
