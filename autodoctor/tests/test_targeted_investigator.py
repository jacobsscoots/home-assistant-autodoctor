from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.investigator import TargetedReadOnlyInvestigator
from autodoctor.models import LogEvent


class FakeMCP:
    def __init__(self, profile: str = "ha-mcp") -> None:
        self.profile = profile
        self.calls: list[tuple[str, dict, str]] = []

    async def health(self):
        return {"enabled": True, "connected": True, "server_profile": self.profile}

    async def call_readonly(self, tool, arguments=None, *, purpose=""):
        self.calls.append((tool, dict(arguments or {}), purpose))
        return {"ok": True, "tool": tool}


def test_investigator_selects_state_history_trace_and_health_without_ai_choice() -> None:
    async def run() -> None:
        mcp = FakeMCP()
        investigator = TargetedReadOnlyInvestigator(mcp)
        event = LogEvent(
            level="ERROR",
            source="homeassistant.components.automation",
            exception="service failed",
            message="automation.kitchen_lights failed because switch.kitchen_plug became unavailable",
            name="homeassistant.components.automation.kitchen_lights",
            timestamp=1.0,
        )
        result = await investigator.collect(event, "automation")
        tools = [name for name, _args, _purpose in mcp.calls]
        assert "ha_get_system_health" in tools
        assert tools.count("ha_get_state") == 2
        assert "ha_get_history" in tools
        assert "ha_get_automation_traces" in tools
        assert "ha_get_integration" in tools
        integration_call = next(call for call in mcp.calls if call[0] == "ha_get_integration")
        assert integration_call[1] == {"domain": "automation"}
        assert result["selection"] == "deterministic-by-incident"
        assert len(result["reads"]) <= 8

    asyncio.run(run())


def test_kasa_uses_tplink_domain_and_keeps_entry_id_private() -> None:
    class KasaMCP(FakeMCP):
        async def call_readonly(self, tool, arguments=None, *, purpose=""):
            self.calls.append((tool, dict(arguments or {}), purpose))
            if tool == "ha_get_integration":
                return {
                    "total_count": 1,
                    "entries": [
                        {
                            "entry_id": "entry1234",
                            "domain": "tplink",
                            "title": "Private plug title",
                            "state": "loaded",
                        }
                    ],
                    "state_summary": {"loaded": 1},
                }
            return {"ok": True, "tool": tool}

    async def run() -> None:
        mcp = KasaMCP()
        investigator = TargetedReadOnlyInvestigator(mcp)
        event = LogEvent(
            level="ERROR",
            source="kasa.transports.klaptransport",
            exception="",
            message="query failed after authentication",
            name="kasa.transports.klaptransport",
            timestamp=1.0,
        )
        ai_evidence, private_evidence = await investigator.collect_split(event, "kasa")

        integration_call = next(call for call in mcp.calls if call[0] == "ha_get_integration")
        assert integration_call[1] == {"domain": "tplink"}
        assert ai_evidence["target_resolution"]["integration_domain"] == "tplink"
        assert ai_evidence["target_resolution"]["candidate_count"] == 1
        assert ai_evidence["target_resolution"]["target_identifier_visibility"] == "private"

        serialized_ai = str(ai_evidence)
        assert "entry1234" not in serialized_ai
        assert "Private plug title" not in serialized_ai
        assert private_evidence["private_target_resolution"]["candidates"] == [
            {"entry_id": "entry1234"}
        ]

    asyncio.run(run())


def test_private_target_resolution_log_exposes_only_safe_cardinality_and_state(caplog) -> None:
    class KasaMCP(FakeMCP):
        async def call_readonly(self, tool, arguments=None, *, purpose=""):
            self.calls.append((tool, dict(arguments or {}), purpose))
            if tool == "ha_get_integration":
                return {
                    "entries": [
                        {
                            "entry_id": "entry1234",
                            "title": "Never log this private title",
                            "domain": "tplink",
                            "state": "loaded",
                        }
                    ]
                }
            return {"ok": True, "tool": tool}

    async def run() -> None:
        caplog.set_level(logging.INFO, logger="autodoctor.investigator")
        investigator = TargetedReadOnlyInvestigator(KasaMCP())
        await investigator.collect_split(
            LogEvent(
                level="ERROR",
                source="kasa.transports.klaptransport",
                exception="",
                message="authentication failed",
                name="kasa.transports.klaptransport",
                timestamp=1.0,
            ),
            "kasa",
        )

    asyncio.run(run())
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Private target resolution domain=tplink candidates=1 states=loaded" in text
    assert "entry1234" not in text
    assert "Never log this private title" not in text


def test_unknown_library_does_not_fall_back_to_fuzzy_integration_search() -> None:
    async def run() -> None:
        mcp = FakeMCP()
        investigator = TargetedReadOnlyInvestigator(mcp)
        event = LogEvent(
            level="ERROR",
            source="aiohttp.client",
            exception="",
            message="connection failed",
            name="aiohttp.client",
            timestamp=1.0,
        )
        ai_evidence, private_evidence = await investigator.collect_split(event, "aiohttp")
        assert all(tool != "ha_get_integration" for tool, _args, _purpose in mcp.calls)
        assert ai_evidence["target_resolution"]["integration_domain"] is None
        assert ai_evidence["target_resolution"]["candidate_count"] == 0
        assert private_evidence["private_target_resolution"]["candidates"] == []

    asyncio.run(run())


def test_investigator_does_not_call_tools_when_mcp_disconnected() -> None:
    class Disconnected(FakeMCP):
        async def health(self):
            return {"enabled": True, "connected": False, "server_profile": "ha-mcp"}

    async def run() -> None:
        mcp = Disconnected()
        investigator = TargetedReadOnlyInvestigator(mcp)
        result = await investigator.collect(
            LogEvent("ERROR", "x", "", "sensor.foo failed", "x", 1.0),
            "x",
        )
        assert result["reads"] == []
        assert mcp.calls == []

    asyncio.run(run())


def test_investigator_never_copies_raw_tool_errors_into_evidence() -> None:
    class Failing(FakeMCP):
        async def call_readonly(self, tool, arguments=None, *, purpose=""):
            self.calls.append((tool, dict(arguments or {}), purpose))
            raise RuntimeError("private-secret-url-should-not-enter-prompt")

    async def run() -> None:
        mcp = Failing()
        investigator = TargetedReadOnlyInvestigator(mcp)
        result = await investigator.collect(
            LogEvent("ERROR", "integration", "", "Connection failed for sensor.foo", "test", 1.0),
            "test",
        )
        serialized = str(result)
        assert "private-secret-url-should-not-enter-prompt" not in serialized
        assert any(read.get("available") is False for read in result["reads"])

    asyncio.run(run())
