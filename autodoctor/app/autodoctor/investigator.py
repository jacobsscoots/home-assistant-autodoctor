from __future__ import annotations

import re
from typing import Any

from .models import LogEvent
from .private_target import entry_ids_from_value, integration_domain_for_event

_ENTITY_RE = re.compile(r"\b(?:automation|script|scene|sensor|binary_sensor|switch|light|climate|cover|lock|alarm_control_panel|input_boolean|input_number|input_select|input_text|timer|person|device_tracker)\.[a-zA-Z0-9_]+\b")


class TargetedReadOnlyInvestigator:
    """Collect narrowly-scoped MCP evidence selected by deterministic rules.

    The AI never chooses these tools. Calls remain subject to MCPBackend's compiled
    read allowlist and the live ha-mcp readOnlyHint gate. Config-entry identifiers are
    separated into private evidence and never serialized into the AI prompt.
    """

    def __init__(self, mcp: Any) -> None:
        self.mcp = mcp

    @staticmethod
    def referenced_entities(event: LogEvent) -> list[str]:
        text = "\n".join((event.name, event.source, event.message, event.exception))
        return list(dict.fromkeys(_ENTITY_RE.findall(text)))[:8]

    @staticmethod
    def _looks_integration_or_system_failure(event: LogEvent) -> bool:
        text = f"{event.name}\n{event.source}\n{event.message}\n{event.exception}".lower()
        markers = (
            "setup failed",
            "error setting up",
            "integration",
            "config entry",
            "authentication",
            "connection",
            "timeout",
            "unavailable",
            "failed",
        )
        return any(marker in text for marker in markers)

    async def _safe_call(self, tool: str, arguments: dict[str, Any], purpose: str) -> dict[str, Any]:
        try:
            result = await self.mcp.call_readonly(tool, arguments, purpose=purpose)
            return {"tool": tool, "available": True, "result": result}
        except Exception:
            return {"tool": tool, "available": False}

    @staticmethod
    def _integration_states(value: Any) -> set[str]:
        states: set[str] = set()
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() == "state" and isinstance(nested, str):
                    states.add(nested.strip().lower())
                states.update(TargetedReadOnlyInvestigator._integration_states(nested))
        elif isinstance(value, (list, tuple)):
            for nested in value:
                states.update(TargetedReadOnlyInvestigator._integration_states(nested))
        return states

    @staticmethod
    def _public_integration_evidence(
        read: dict[str, Any],
        domain: str,
        candidate_ids: set[str],
    ) -> dict[str, Any]:
        if not read.get("available"):
            return {
                "tool": "ha_get_integration",
                "available": False,
                "result": {
                    "integration_domain": domain,
                    "candidate_count": 0,
                    "target_identifier_visibility": "private",
                },
            }
        states = sorted(TargetedReadOnlyInvestigator._integration_states(read.get("result")))
        return {
            "tool": "ha_get_integration",
            "available": True,
            "result": {
                "integration_domain": domain,
                "candidate_count": len(candidate_ids),
                "candidate_states": states[:20],
                "target_identifier_visibility": "private",
            },
        }

    async def _collect_ha_mcp(
        self, event: LogEvent, family: str, entities: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        reads: list[dict[str, Any]] = []
        private_target: dict[str, Any] = {
            "integration_domain": None,
            "candidates": [],
        }
        if self._looks_integration_or_system_failure(event):
            reads.append(
                await self._safe_call(
                    "ha_get_system_health", {}, "targeted incident system health evidence"
                )
            )

        for entity_id in entities[:3]:
            reads.append(
                await self._safe_call(
                    "ha_get_state",
                    {"entity_id": entity_id},
                    "targeted incident entity state evidence",
                )
            )

        if entities:
            reads.append(
                await self._safe_call(
                    "ha_get_history",
                    {"entity_ids": entities[:3], "start_time": "2h"},
                    "targeted recent state history evidence",
                )
            )

        automation_ids = [
            entity.split(".", 1)[1]
            for entity in entities
            if entity.startswith("automation.")
        ]
        for automation_id in automation_ids[:2]:
            reads.append(
                await self._safe_call(
                    "ha_get_automation_traces",
                    {"automation_id": automation_id},
                    "targeted automation trace evidence",
                )
            )

        domain = integration_domain_for_event(event, family)
        if domain:
            integration_read = await self._safe_call(
                "ha_get_integration",
                {"domain": domain},
                "private deterministic config-entry resolution",
            )
            candidate_ids = (
                entry_ids_from_value(integration_read.get("result"))
                if integration_read.get("available")
                else set()
            )
            private_target = {
                "integration_domain": domain,
                "candidates": [{"entry_id": entry_id} for entry_id in sorted(candidate_ids)],
            }
            reads.append(self._public_integration_evidence(integration_read, domain, candidate_ids))

        return reads, private_target

    async def _collect_ganhammar(self, entities: list[str]) -> list[dict[str, Any]]:
        reads: list[dict[str, Any]] = []
        for entity_id in entities[:3]:
            reads.append(
                await self._safe_call(
                    "get_state",
                    {"entity_id": entity_id},
                    "targeted incident entity state evidence",
                )
            )
        return reads

    async def collect_split(
        self, event: LogEvent, family: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return AI-safe evidence and separate private executor evidence."""

        try:
            health = await self.mcp.health()
        except Exception:
            return {"enabled": False, "reads": []}, {"private_target_resolution": {"candidates": []}}
        if not health.get("enabled") or not health.get("connected"):
            return (
                {"enabled": bool(health.get("enabled")), "reads": []},
                {"private_target_resolution": {"candidates": []}},
            )

        profile = str(health.get("server_profile") or "unknown")
        entities = self.referenced_entities(event)
        private_target: dict[str, Any] = {"integration_domain": None, "candidates": []}
        if profile == "ha-mcp":
            reads, private_target = await self._collect_ha_mcp(event, family, entities)
        elif profile == "ganhammar":
            reads = await self._collect_ganhammar(entities)
        else:
            reads = []

        ai_evidence = {
            "enabled": True,
            "server_profile": profile,
            "selection": "deterministic-by-incident",
            "referenced_entity_count": len(entities),
            "target_resolution": {
                "integration_domain": private_target.get("integration_domain"),
                "candidate_count": len(private_target.get("candidates") or []),
                "target_identifier_visibility": "private",
            },
            "reads": reads[:8],
        }
        private_evidence = {
            "ai_evidence": ai_evidence,
            "private_target_resolution": private_target,
        }
        return ai_evidence, private_evidence

    async def collect(self, event: LogEvent, family: str) -> dict[str, Any]:
        """Backwards-compatible AI-safe evidence view."""

        ai_evidence, _private = await self.collect_split(event, family)
        return ai_evidence
