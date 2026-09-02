from __future__ import annotations

import asyncio
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
        assert result["selection"] == "deterministic-by-incident"
        assert len(result["reads"]) <= 8

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
