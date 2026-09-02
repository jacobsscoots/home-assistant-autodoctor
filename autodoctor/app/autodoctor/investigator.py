from __future__ import annotations

import re
from typing import Any

from .models import LogEvent

_ENTITY_RE = re.compile(r"\b(?:automation|script|scene|sensor|binary_sensor|switch|light|climate|cover|lock|alarm_control_panel|input_boolean|input_number|input_select|input_text|timer|person|device_tracker)\.[a-zA-Z0-9_]+\b")


class TargetedReadOnlyInvestigator:
    """Collect narrowly-scoped MCP evidence selected by deterministic rules.

    The AI never chooses these tools. Calls remain subject to MCPBackend's compiled
    read allowlist and the live ha-mcp readOnlyHint gate. Failures are represented as
    unavailable evidence without copying raw transport errors into the AI prompt.
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

    async def collect(self, event: LogEvent, family: str) -> dict[str, Any]:
        try:
            health = await self.mcp.health()
        except Exception:
            return {"enabled": False, "reads": []}
        if not health.get("enabled") or not health.get("connected"):
            return {"enabled": bool(health.get("enabled")), "reads": []}

        profile = str(health.get("server_profile") or "unknown")
        entities = self.referenced_entities(event)
        reads: list[dict[str, Any]] = []

        if profile == "ha-mcp":
            if self._looks_integration_or_system_failure(event):
                reads.append(
                    await self._safe_call(
                        "ha_get_system_health",
                        {},
                        "targeted incident system health evidence",
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

            automation_ids = [entity.split(".", 1)[1] for entity in entities if entity.startswith("automation.")]
            for automation_id in automation_ids[:2]:
                reads.append(
                    await self._safe_call(
                        "ha_get_automation_traces",
                        {"automation_id": automation_id},
                        "targeted automation trace evidence",
                    )
                )

            if family and family != "unknown":
                reads.append(
                    await self._safe_call(
                        "ha_get_integration",
                        {"query": family},
                        "targeted integration metadata evidence",
                    )
                )

        elif profile == "ganhammar":
            for entity_id in entities[:3]:
                reads.append(
                    await self._safe_call(
                        "get_state",
                        {"entity_id": entity_id},
                        "targeted incident entity state evidence",
                    )
                )

        # Bound the number of evidence records independently of MCP result bounding.
        return {
            "enabled": True,
            "server_profile": profile,
            "selection": "deterministic-by-incident",
            "referenced_entity_count": len(entities),
            "reads": reads[:8],
        }
