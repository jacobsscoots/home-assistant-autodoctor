from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.investigator import TargetedReadOnlyInvestigator
from autodoctor.models import LogEvent
from autodoctor.private_target import private_rfc1918_ipv4s_for_event


class KasaMCP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    async def health(self):
        return {"enabled": True, "connected": True, "server_profile": "ha-mcp"}

    async def call_readonly(self, tool, arguments=None, *, purpose=""):
        self.calls.append((tool, dict(arguments or {}), purpose))
        if tool == "ha_get_integration":
            return {
                "entries": [
                    {"entry_id": "entry1111", "domain": "tplink", "state": "loaded"},
                    {"entry_id": "entry2222", "domain": "tplink", "state": "loaded"},
                ]
            }
        return {"ok": True, "tool": tool}


class MatchingHA:
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def match_tplink_config_entries_by_host(self, host: str):
        self.hosts.append(host)
        return {
            "domain": "tplink",
            "count": 1,
            "matches": [{"entry_id": "entry2222", "state": "loaded"}],
        }


class NoMatchHA(MatchingHA):
    async def match_tplink_config_entries_by_host(self, host: str):
        self.hosts.append(host)
        return {"domain": "tplink", "count": 0, "matches": []}


def _event(message: str) -> LogEvent:
    return LogEvent(
        level="ERROR",
        source="kasa.transports.klaptransport",
        exception="",
        message=message,
        name="kasa.transports.klaptransport",
        timestamp=1.0,
    )


def test_private_ipv4_extraction_accepts_only_unique_rfc1918_literals() -> None:
    event = _event("failed talking to 192.168.50.21; external peer 8.8.8.8")
    assert private_rfc1918_ipv4s_for_event(event) == ["192.168.50.21"]


def test_exact_private_host_refines_two_tplink_candidates_to_one_without_ai_leak(caplog) -> None:
    async def run() -> None:
        caplog.set_level(logging.INFO, logger="autodoctor.investigator")
        mcp = KasaMCP()
        ha = MatchingHA()
        investigator = TargetedReadOnlyInvestigator(mcp, ha)
        ai_evidence, private_evidence = await investigator.collect_split(
            _event("authentication failed for 192.168.50.21"),
            "kasa",
        )

        assert ha.hosts == ["192.168.50.21"]
        assert ai_evidence["target_resolution"]["candidate_count"] == 1
        assert ai_evidence["target_resolution"]["resolution_method"] == "exact_private_host"
        assert private_evidence["private_target_resolution"]["candidates"] == [
            {"entry_id": "entry2222"}
        ]

        serialized_ai = str(ai_evidence)
        assert "192.168.50.21" not in serialized_ai
        assert "entry1111" not in serialized_ai
        assert "entry2222" not in serialized_ai

    asyncio.run(run())
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "192.168.50.21" not in logs
    assert "entry1111" not in logs
    assert "entry2222" not in logs
    assert "before=2 after=1 result=matched" in logs


def test_zero_host_matches_leave_original_ambiguity_fail_closed() -> None:
    async def run() -> None:
        ha = NoMatchHA()
        investigator = TargetedReadOnlyInvestigator(KasaMCP(), ha)
        ai_evidence, private_evidence = await investigator.collect_split(
            _event("timeout talking to 192.168.50.21"),
            "kasa",
        )
        assert ai_evidence["target_resolution"]["candidate_count"] == 2
        assert ai_evidence["target_resolution"]["resolution_method"] == "integration_domain"
        assert len(private_evidence["private_target_resolution"]["candidates"]) == 2

    asyncio.run(run())


def test_public_or_multiple_ipv4_signals_do_not_invoke_private_host_bridge() -> None:
    async def run() -> None:
        ha = MatchingHA()
        investigator = TargetedReadOnlyInvestigator(KasaMCP(), ha)

        public_result, _ = await investigator.collect_split(
            _event("timeout talking to 8.8.8.8"),
            "kasa",
        )
        assert public_result["target_resolution"]["candidate_count"] == 2
        assert ha.hosts == []

        multi_result, _ = await investigator.collect_split(
            _event("traffic between 192.168.50.21 and 192.168.50.22 failed"),
            "kasa",
        )
        assert multi_result["target_resolution"]["candidate_count"] == 2
        assert ha.hosts == []

    asyncio.run(run())
