from __future__ import annotations

import asyncio
import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.cases import IncidentCaseManager
from autodoctor.config import Settings
from autodoctor.engine import AutoDoctorEngine
from autodoctor.fingerprint import fingerprint
from autodoctor.memory import pattern_signature
from autodoctor.models import LogEvent
from autodoctor.scheduler import incident_family
from autodoctor.store import IncidentStore


def template_event(owner: str, *, name: str = "homeassistant.helpers.script") -> LogEvent:
    return LogEvent(
        "ERROR", "helpers/script.py", "",
        f"{owner}: Error executing script. Error rendering template for call_service at pos 1: TypeError",
        name, 1000,
    )


def key(event: LogEvent) -> str:
    return pattern_signature(event, incident_family(event.name, event.source))[0]


@pytest.mark.parametrize("component", ["automation", "script"])
def test_template_groups_preserve_specific_controller_logger(component: str) -> None:
    a = template_event("Same alias", name=f"homeassistant.components.{component}.diagnostic12")
    b = replace(a, name=f"homeassistant.components.{component}.diagnostic13")
    assert key(a) != key(b)


@pytest.mark.parametrize("owners", [("Diagnostic One", "Diagnostic Two"), ("Diagnostic 12", "Diagnostic 13"), ("Diagnostic: One", "Diagnostic: Two")])
def test_template_groups_preserve_owning_alias(owners: tuple[str, str]) -> None:
    assert key(template_event(owners[0])) != key(template_event(owners[1]))


def test_same_owner_ignores_changing_script_steps_and_runtime_values() -> None:
    a = template_event("Diagnostic One")
    b = replace(a, message="Diagnostic One: Choose at step 12: Error executing script. Error rendering template at pos 7: TypeError", timestamp=2000)
    assert key(a) == key(b)


def test_unknown_owner_does_not_collapse_distinct_template_evidence() -> None:
    a = template_event("")
    a = replace(a, message="Error rendering template for sensor.example12")
    b = replace(a, message="Error rendering template for sensor.example13")
    assert key(a) != key(b)


def test_non_template_broad_grouping_is_unchanged() -> None:
    event = LogEvent("ERROR", "test.py", "", "Device query TimeoutError after 12 seconds", "kasa.smart", 1000)
    family, label = "kasa", "device_query_timeout"
    digest = hashlib.sha256(f"{family}|{label}".encode()).hexdigest()[:10]
    assert pattern_signature(event, family) == (f"{family}/{label}/{digest}", label)


def test_template_upgrade_preserves_legacy_history_and_starts_clean_cases(tmp_path: Path) -> None:
    async def run() -> None:
        path = str(tmp_path / "history.db")
        store = IncidentStore(path)
        await store.initialize()
        cases = IncidentCaseManager(path, None, notifications_enabled=False)
        await cases.initialize()
        a, b = template_event("Diagnostic 12"), template_event("Diagnostic 13")
        family, label = incident_family(a.name), "template_error"
        old_key = f"{family}/{label}/" + hashlib.sha256(f"{family}|{label}".encode()).hexdigest()[:10]
        old_fp = fingerprint(a)
        for _ in range(2):
            _, is_new = await store.record(old_fp, a, old_key, label)
            await cases.record_event(pattern_key=old_key, pattern_label=label, family=family, fingerprint=old_fp, event=a, fingerprint_is_new=is_new)
        await cases._set_status(old_key, "needs_user_action")
        engine = AutoDoctorEngine(Settings(), store, None, None, None)
        for event in (a, a, b):
            fp, fam, pattern, pattern_label, _, is_new = await engine._record_incident(event)
            assert fp != old_fp
            await cases.record_event(pattern_key=pattern, pattern_label=pattern_label, family=fam, fingerprint=fp, event=event, fingerprint_is_new=is_new)
        rows = await store.list_recent(10)
        assert len(rows) == 3
        assert sum(row["occurrences"] for row in rows) == 5
        old_row = next(row for row in rows if row["fingerprint"] == old_fp)
        assert old_row["pattern_key"] == old_key
        assert old_row["occurrences"] == 2
        for _ in range(2):
            await cases.reconcile_backlog(rows)
        current = await cases.list_cases(10)
        assert len(current) == 3
        assert sum(case["occurrences"] for case in current) == 5
        legacy = await cases.get_case(old_key)
        assert legacy is not None and legacy["status"] == "needs_user_action"
        assert all(case["repair_plan_id"] is None for case in current if case["pattern_key"] != old_key)

    asyncio.run(run())
