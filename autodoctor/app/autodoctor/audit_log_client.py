"""Fixed native script editor/trace reads for the reviewed encoding repair only."""
from __future__ import annotations

import ast
import base64
import json

from typing import Any

from .audit_log_recipe import ENTITY, KEY
from .diagnostic_recipe import DiagnosticHAClient
from .repair_backup import RepairBlocked
from .repair_journal import config_digest


class AuditLogHAClient:
    def __init__(self, ha: Any, settings: Any) -> None:
        self.ha, self.settings = ha, settings

    async def _identity(self) -> str:
        entity = self.settings.audit_log_repair_entity
        if not isinstance(entity, str) or not ENTITY.fullmatch(entity):
            raise RepairBlocked("audit_log_target_not_enrolled")
        result = await self.ha._repair_read({"type": "config/entity_registry/get", "entity_id": entity})
        if not isinstance(result, dict) or result.get("entity_id") != entity or result.get("platform") != "script":
            raise RepairBlocked("audit_log_registry_identity_unavailable")
        key = result.get("unique_id")
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise RepairBlocked("audit_log_script_key_invalid")
        return key

    async def idle(self, key: str) -> None:
        if await self._identity() != key:
            raise RepairBlocked("audit_log_identity_changed")
        state = await self.ha.get_state(self.settings.audit_log_repair_entity)
        attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
        if (not isinstance(state, dict) or state.get("state") != "off"
                or type(attrs.get("current")) is not int or attrs["current"] != 0):
            raise RepairBlocked("audit_log_busy_or_run_count_unknown")

    async def _loaded(self) -> dict[str, Any]:
        result = await self.ha._repair_read({"type": "script/config", "entity_id": self.settings.audit_log_repair_entity})
        config = result.get("config") if isinstance(result, dict) else None
        if not isinstance(config, dict):
            raise RepairBlocked("audit_log_loaded_config_unavailable")
        return config

    async def resolve(self) -> tuple[str, dict[str, Any]]:
        # Reload semantics were reviewed against this release. Future versions require
        # requalification, not an assumption that the native editor is unchanged.
        if await self.ha.get_version() != "2026.9.3":
            raise RepairBlocked("audit_log_recipe_requires_reviewed_core_2026_9_3")
        key = await self._identity()
        await self.idle(key)
        loaded, stored = await self._loaded(), await self.read(key)
        if config_digest(loaded) != config_digest(stored):
            raise RepairBlocked("audit_log_loaded_and_editor_config_differ")
        return key, stored

    async def read(self, key: str) -> dict[str, Any]:
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise RepairBlocked("audit_log_invalid_script_key")
        url = f"{self.ha.api_base}/config/script/config/{key}"
        async with self.ha.session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise RepairBlocked("audit_log_editor_source_unavailable")
            result = await response.json()
        if not isinstance(result, dict):
            raise RepairBlocked("audit_log_editor_config_invalid")
        return result

    async def write_checked(self, key: str, expected: dict[str, Any], updated: dict[str, Any]) -> None:
        if await self.ha.get_version() != "2026.9.3":
            raise RepairBlocked("audit_log_core_changed_before_write")
        if config_digest(await self.read(key)) != config_digest(expected):
            raise RepairBlocked("audit_log_config_changed_no_overwrite")
        if config_digest(await self._loaded()) != config_digest(expected):
            raise RepairBlocked("audit_log_loaded_config_changed_no_overwrite")
        await self.idle(key)
        # HA has no cross-editor CAS or idle-and-save transaction. No concurrent edits
        # are permitted during enrollment. A new run can still race this last check.
        url = f"{self.ha.api_base}/config/script/config/{key}"
        async with self.ha.session.post(url, json=updated, allow_redirects=False) as response:
            if response.status != 200:
                raise RepairBlocked("audit_log_save_rejected_or_uncertain")
            result = await response.json()
        if not isinstance(result, dict) or result.get("result") != "ok":
            raise RepairBlocked("audit_log_save_outcome_uncertain")
        if config_digest(await self.read(key)) != config_digest(updated):
            raise RepairBlocked("audit_log_postwrite_readback_mismatch")

    async def natural_run_verified(self, key: str, since: float, expected: dict[str, Any]) -> bool:
        traces = await self.ha._repair_read({"type": "trace/list", "domain": "script", "item_id": key})
        if not isinstance(traces, list):
            raise RepairBlocked("audit_log_trace_list_unavailable")
        for trace in traces[:20]:
            if not isinstance(trace, dict) or not DiagnosticHAClient._completed_trace(trace, since):
                continue
            run_id = trace.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            detail = await self.ha._repair_read({"type": "trace/get", "domain": "script", "item_id": key, "run_id": run_id})
            if self._verified_detail(detail, key, since, expected):
                return True
        return False

    @staticmethod
    def _verified_detail(detail: Any, key: str, since: float, expected: dict[str, Any]) -> bool:
        if not isinstance(detail, dict) or not DiagnosticHAClient._completed_trace(detail, since):
            return False
        if detail.get("domain") != "script" or detail.get("item_id") != key:
            return False
        config = detail.get("config")
        if not isinstance(config, dict) or config_digest(config) != config_digest(expected):
            return False
        # Without response_variable, HA does not retain a shell returncode. Verify the
        # actual scope of this recipe: the formerly failing JSON reaches the unchanged
        # shell argument intact. This is NOT a claim that the helper appended to disk.
        steps = detail.get("trace")
        if not isinstance(steps, dict):
            return False
        variables: dict[str, Any] = {}
        terminal = None
        for path in ("sequence/0", "sequence/1"):
            entries = steps.get(path)
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                return False
            step = entries[0]
            if step.get("error"):
                return False
            changed = step.get("changed_variables", {})
            if not isinstance(changed, dict):
                return False
            variables.update(changed)
            terminal = step
        return _encoding_proven(terminal, variables, expected)


def _encoding_proven(terminal: dict[str, Any], variables: dict[str, Any], config: dict[str, Any]) -> bool:
    result = terminal.get("result")
    params = result.get("params") if isinstance(result, dict) else None
    if not isinstance(params, dict) or params.get("domain") != "shell_command":
        return False
    action = config["sequence"][1]
    service = action.get("action", action.get("service"))
    if service != "shell_command." + str(params.get("service", "")):
        return False
    data = params.get("service_data")
    encoded = data.get("b64") if isinstance(data, dict) else None
    if not isinstance(encoded, str) or len(encoded) > 65536 or encoded != variables.get("b64"):
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        payload = json.loads(decoded)
        # A null/boolean-containing payload already worked before the repair. Require
        # a natural run of the previously broken, Python-literal-compatible shape.
        legacy_object = ast.literal_eval(decoded)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return False
    original = variables.get("payload")
    if not isinstance(payload, dict) or not isinstance(legacy_object, dict) or not isinstance(original, dict):
        return False
    # JSON canonicalization also distinguishes bool/int and int/float values, unlike
    # Python container equality. No payload or encoded argument is persisted/logged.
    return json.dumps(payload, sort_keys=True) == json.dumps(original, sort_keys=True)
