from __future__ import annotations

import logging
import re
from typing import Any

from .models import LogEvent
from .private_target import (
    entry_ids_from_value,
    integration_domain_for_event,
    private_rfc1918_ipv4s_for_event,
)

_LOG = logging.getLogger(__name__)
_ENTITY_RE = re.compile(r"\b(?:automation|script|scene|sensor|binary_sensor|switch|light|climate|cover|lock|alarm_control_panel|input_boolean|input_number|input_select|input_text|timer|person|device_tracker)\.[a-zA-Z0-9_]+\b")


class TargetedReadOnlyInvestigator:
    """Collect narrowly-scoped MCP evidence selected by deterministic rules.

    The AI never chooses these tools. Calls remain subject to MCPBackend's compiled
    read allowlist and the live ha-mcp readOnlyHint gate. Config-entry identifiers and
    private incident host signals are separated from AI-visible evidence.
    """

    def __init__(self, mcp: Any, ha: Any | None = None) -> None:
        self.mcp = mcp
        self.ha = ha

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
        resolution_method: str,
    ) -> dict[str, Any]:
        if not read.get("available"):
            return {
                "tool": "ha_get_integration",
                "available": False,
                "result": {
                    "integration_domain": domain,
                    "candidate_count": 0,
                    "resolution_method": resolution_method,
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
                "resolution_method": resolution_method,
                "target_identifier_visibility": "private",
            },
        }

    @staticmethod
    def _log_private_target_resolution(public_evidence: dict[str, Any]) -> None:
        """Emit only the already-sanitized target-resolution cardinality/status."""

        result = public_evidence.get("result") if isinstance(public_evidence, dict) else {}
        if not isinstance(result, dict):
            result = {}
        domain = str(result.get("integration_domain") or "none")[:100]
        method = str(result.get("resolution_method") or "integration_domain")[:100]
        try:
            count = max(0, int(result.get("candidate_count") or 0))
        except (TypeError, ValueError):
            count = 0
        raw_states = result.get("candidate_states") or []
        states = [str(state)[:80] for state in raw_states if isinstance(state, str)][:20]
        _LOG.info(
            "Private target resolution domain=%s candidates=%d states=%s method=%s",
            domain,
            count,
            ",".join(states) if states else "none",
            method,
        )

    async def _query_private_tplink_host(self, host: str) -> dict[str, Any]:
        """Call the fixed HA resolver without creating a permanent second HA client."""

        if self.ha is not None:
            return await self.ha.match_tplink_config_entries_by_host(host)

        # CaseAwareAutoDoctorEngine historically constructed the investigator with
        # only MCP. Preserve that API while using the existing Supervisor credential
        # for this fixed read-only Home Assistant command. The temporary client is
        # always closed and does not create a generic HA command surface.
        from .ha import HomeAssistantClient

        client = HomeAssistantClient()
        try:
            return await client.match_tplink_config_entries_by_host(host)
        finally:
            await client.close()

    async def _refine_tplink_candidates(
        self,
        event: LogEvent,
        candidate_ids: set[str],
    ) -> tuple[set[str], str]:
        """Privately narrow ambiguous TP-Link entries using exact stored-host equality.

        The raw IPv4 and returned config-entry identifier never enter logs or AI-safe
        evidence. Any missing helper, malformed result, zero/multiple match, or mismatch
        against the MCP candidate set leaves the original candidate set untouched.
        """

        if len(candidate_ids) <= 1:
            return candidate_ids, "integration_domain"

        private_hosts = private_rfc1918_ipv4s_for_event(event)
        if len(private_hosts) != 1:
            _LOG.info(
                "Private target host refinement domain=tplink before=%d result=no_single_private_ipv4",
                len(candidate_ids),
            )
            return candidate_ids, "integration_domain"

        try:
            result = await self._query_private_tplink_host(private_hosts[0])
        except Exception:
            _LOG.info(
                "Private target host refinement domain=tplink before=%d result=unavailable",
                len(candidate_ids),
            )
            return candidate_ids, "integration_domain"

        matched_ids = entry_ids_from_value(result)
        try:
            declared_count = int(result.get("count")) if isinstance(result, dict) else -1
        except (TypeError, ValueError):
            declared_count = -1

        if declared_count != 1 or len(matched_ids) != 1 or not matched_ids.issubset(candidate_ids):
            _LOG.info(
                "Private target host refinement domain=tplink before=%d result=no_unique_verified_match",
                len(candidate_ids),
            )
            return candidate_ids, "integration_domain"

        _LOG.info(
            "Private target host refinement domain=tplink before=%d after=1 result=matched",
            len(candidate_ids),
        )
        return matched_ids, "exact_private_host"

    async def _collect_ha_mcp(
        self, event: LogEvent, family: str, entities: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        reads: list[dict[str, Any]] = []
        private_target: dict[str, Any] = {
            "integration_domain": None,
            "candidates": [],
            "resolution_method": "none",
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
            resolution_method = "integration_domain"
            if domain == "tplink" and candidate_ids:
                candidate_ids, resolution_method = await self._refine_tplink_candidates(
                    event,
                    candidate_ids,
                )
            private_target = {
                "integration_domain": domain,
                "candidates": [{"entry_id": entry_id} for entry_id in sorted(candidate_ids)],
                "resolution_method": resolution_method,
            }
            public_evidence = self._public_integration_evidence(
                integration_read,
                domain,
                candidate_ids,
                resolution_method,
            )
            reads.append(public_evidence)
            self._log_private_target_resolution(public_evidence)

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
        private_target: dict[str, Any] = {
            "integration_domain": None,
            "candidates": [],
            "resolution_method": "none",
        }
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
                "resolution_method": private_target.get("resolution_method"),
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
