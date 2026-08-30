from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.memory import pattern_signature
from autodoctor.models import Analysis, LogEvent
from autodoctor.scheduler import incident_family
from autodoctor.store import IncidentStore


def event(
    message: str,
    *,
    name: str = "kasa.smart.smartdevice",
    source: str = "core.py",
    timestamp: float = 1000.0,
) -> LogEvent:
    return LogEvent("ERROR", source, "", message, name, timestamp)


def analysis(root: str = "Transient device transport timeout") -> Analysis:
    return Analysis(
        summary="Observe before changing configuration",
        root_cause=root,
        confidence=0.65,
        risk="low",
        action="observe",
        checks=["Check whether the device recovered without intervention"],
        proposed_changes=[],
        raw={
            "summary": "Observe before changing configuration",
            "root_cause": root,
            "confidence": 0.65,
            "risk": "low",
            "action": "observe",
            "affected_files": [],
            "checks": ["Check whether the device recovered without intervention"],
            "proposed_changes": [],
        },
    )


def test_broad_pattern_groups_changing_kasa_timeout_details() -> None:
    a = event("Error querying 192.168.1.10 for modules Time, Cloud: TimeoutError()")
    b = event("Error querying 192.168.1.99 for modules Matter, DeviceModule: TimeoutError()")
    family = incident_family(a.name, a.source)
    key_a, label_a = pattern_signature(a, family)
    key_b, label_b = pattern_signature(b, family)
    assert family == "kasa"
    assert label_a == label_b == "device_query_timeout"
    assert key_a == key_b


def test_stable_random_alias_survives_store_restart(tmp_path: Path) -> None:
    async def run() -> tuple[str, str]:
        path = tmp_path / "aliases.db"
        first = IncidentStore(str(path))
        await first.initialize()
        alias1 = (await first.get_or_create_entity_aliases(["sensor.private_temperature"], 1000))["sensor.private_temperature"]

        second = IncidentStore(str(path))
        await second.initialize()
        alias2 = (await second.get_or_create_entity_aliases(["sensor.private_temperature"], 2000))["sensor.private_temperature"]
        return alias1, alias2

    alias1, alias2 = asyncio.run(run())
    assert alias1 == alias2
    assert alias1.startswith("sensor.entity_")
    assert "private_temperature" not in alias1


def test_verified_seed_outranks_ai_hypothesis_and_fts_finds_fix(tmp_path: Path) -> None:
    async def run() -> list[dict]:
        store = IncidentStore(str(tmp_path / "rank.db"))
        await store.initialize()
        await store.save_ai_memory(
            fingerprint="ai-fp",
            family="kasa",
            pattern_key="kasa/device_query_timeout/example",
            pattern_label="device_query_timeout",
            analysis=analysis("Maybe change a timeout setting"),
            occurrences=2,
            ha_version="2026.8.3",
            autodoctor_version="0.1.5",
            expiry_days=30,
            now_ts=1_800_000_000,
        )
        result = await store.retrieve_memory(
            query_text="morning Tapo device timeout can derail routine",
            family="kasa",
            pattern_key="different-pattern",
            pattern_label="device_query_timeout",
            aliases=[],
            limit=5,
            max_chars=6000,
            now_ts=1_800_000_100,
        )
        return result["knowledge"]

    items = asyncio.run(run())
    keys = [item["memory_key"] for item in items]
    assert "seed:morning-tapo-timeout" in keys
    assert keys.index("seed:morning-tapo-timeout") < keys.index("ai:ai-fp")
    assert next(item for item in items if item["memory_key"] == "seed:morning-tapo-timeout")["trust_class"] == "verified_fix"


def test_expired_ai_hypothesis_is_not_retrieved(tmp_path: Path) -> None:
    async def run() -> list[dict]:
        store = IncidentStore(str(tmp_path / "expiry.db"))
        await store.initialize()
        await store.save_ai_memory(
            fingerprint="old-fp",
            family="custom_components.example",
            pattern_key="custom_components.example/timeout/x",
            pattern_label="timeout",
            analysis=analysis(),
            occurrences=2,
            ha_version="2026.8.3",
            autodoctor_version="0.1.5",
            expiry_days=1,
            now_ts=1000,
        )
        result = await store.retrieve_memory(
            query_text="timeout",
            family="custom_components.example",
            pattern_key="custom_components.example/timeout/x",
            pattern_label="timeout",
            aliases=[],
            limit=10,
            max_chars=6000,
            now_ts=1000 + 2 * 86400,
        )
        return result["knowledge"]

    assert all(item["memory_key"] != "ai:old-fp" for item in asyncio.run(run()))


def test_recurrence_feedback_moves_from_continued_to_worsened(tmp_path: Path) -> None:
    async def run() -> tuple[str, int]:
        store = IncidentStore(str(tmp_path / "outcomes.db"))
        await store.initialize()
        await store.save_ai_memory(
            fingerprint="fp",
            family="kasa",
            pattern_key="kasa/device_query_timeout/x",
            pattern_label="device_query_timeout",
            analysis=analysis(),
            occurrences=2,
            ha_version="2026.8.3",
            autodoctor_version="0.1.5",
            expiry_days=30,
            now_ts=1000,
        )
        await store.record_recurrence_outcome("fp", 3, worsened_recurrences=3, now_ts=1100)
        first = await store.retrieve_memory(
            query_text="timeout",
            family="kasa",
            pattern_key="kasa/device_query_timeout/x",
            pattern_label="device_query_timeout",
            aliases=[], limit=10, max_chars=6000, now_ts=1101,
        )
        item = next(x for x in first["knowledge"] if x["memory_key"] == "ai:fp")
        assert item["outcome"] == "continued"

        await store.record_recurrence_outcome("fp", 5, worsened_recurrences=3, now_ts=1200)
        second = await store.retrieve_memory(
            query_text="timeout",
            family="kasa",
            pattern_key="kasa/device_query_timeout/x",
            pattern_label="device_query_timeout",
            aliases=[], limit=10, max_chars=6000, now_ts=1201,
        )
        item = next(x for x in second["knowledge"] if x["memory_key"] == "ai:fp")
        return item["outcome"], item["recurrence_count"]

    outcome, count = asyncio.run(run())
    assert outcome == "worsened"
    assert count == 3


def test_quiet_feedback_is_cautious_not_fixed(tmp_path: Path) -> None:
    async def run() -> str:
        store = IncidentStore(str(tmp_path / "quiet.db"))
        await store.initialize()
        ev = event("Device timeout", timestamp=900)
        key, label = pattern_signature(ev, "kasa")
        await store.record("quiet-fp", ev, key, label)
        await store.save_ai_memory(
            fingerprint="quiet-fp",
            family="kasa",
            pattern_key=key,
            pattern_label=label,
            analysis=analysis(),
            occurrences=1,
            ha_version="2026.8.3",
            autodoctor_version="0.1.5",
            expiry_days=30,
            now_ts=1000,
        )
        await store.refresh_quiet_outcomes(86400, now_ts=1000 + 90000)
        result = await store.retrieve_memory(
            query_text="Device timeout",
            family="kasa",
            pattern_key=key,
            pattern_label=label,
            aliases=[], limit=10, max_chars=6000, now_ts=1000 + 90001,
        )
        return next(x for x in result["knowledge"] if x["memory_key"] == "ai:quiet-fp")["outcome"]

    assert asyncio.run(run()) == "quiet"


def test_topology_contains_only_aliases_and_observed_relationships(tmp_path: Path) -> None:
    async def run() -> tuple[list[dict], dict[str, str]]:
        store = IncidentStore(str(tmp_path / "topology.db"))
        await store.initialize()
        raw = ["automation.private_routine", "sensor.private_sensor", "input_boolean.private_helper"]
        aliases = await store.get_or_create_entity_aliases(raw, 1000)
        await store.observe_topology(raw, aliases, "homeassistant.components.test", 1000)
        result = await store.retrieve_memory(
            query_text="routine sensor helper",
            family="homeassistant.components.test",
            pattern_key="",
            pattern_label="other",
            aliases=list(aliases.values()),
            limit=5,
            max_chars=6000,
            now_ts=1001,
        )
        return result["topology"], aliases

    topology, aliases = asyncio.run(run())
    serialized = json.dumps(topology)
    assert "private_routine" not in serialized
    assert "private_sensor" not in serialized
    assert "private_helper" not in serialized
    controller = aliases["automation.private_routine"]
    assert any(edge["source"] == controller and edge["relation"] == "references_in_incident" for edge in topology)


def test_v013_incident_schema_migrates_additively_and_backfills_pattern(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE incidents (
                fingerprint TEXT PRIMARY KEY,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                occurrences INTEGER NOT NULL,
                level TEXT NOT NULL,
                name TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                exception TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                last_analysis_at REAL,
                analysis_json TEXT
            );
            """
        )
        db.execute(
            """INSERT INTO incidents
            VALUES ('fp', 1, 2, 7, 'ERROR', 'kasa.smart.smartdevice', 'core.py',
                    'Error querying device: TimeoutError()', '', 'open', NULL, NULL)"""
        )
        db.commit()

    store = IncidentStore(str(path))
    asyncio.run(store.initialize())

    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(incidents)").fetchall()}
        assert {"pattern_key", "pattern_label"}.issubset(columns)
        row = db.execute("SELECT occurrences, pattern_key, pattern_label FROM incidents WHERE fingerprint='fp'").fetchone()
        assert row[0] == 7
        assert row[1].startswith("kasa/device_query_timeout/")
        assert row[2] == "device_query_timeout"


def test_memory_health_exposes_trust_topology_alias_and_fts_state(tmp_path: Path) -> None:
    async def run() -> dict:
        store = IncidentStore(str(tmp_path / "health.db"))
        await store.initialize()
        await store.get_or_create_entity_aliases(["sensor.private"], 1000)
        return await store.memory_health(now_ts=1001)

    health = asyncio.run(run())
    assert health["knowledge_total"] >= 7
    assert health["by_trust"]["verified_fix"] >= 1
    assert health["stable_entity_aliases"] == 1
    assert isinstance(health["fts5_available"], bool)
