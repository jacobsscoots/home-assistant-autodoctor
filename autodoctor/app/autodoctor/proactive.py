"""Bounded native reads of enrolled targets; observations are not system-log events."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime
from typing import Any

from .database import database_connection
from .models import Analysis, LogEvent

_ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_ENTRY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS health_observations (
    target_key TEXT PRIMARY KEY, fault TEXT NOT NULL, first_seen REAL NOT NULL,
    last_seen REAL NOT NULL, confirmations INTEGER NOT NULL
);
"""


def observation_identity(kind: str, target: str) -> tuple[str, str]:
    digest = hashlib.sha256((kind + "\0" + target).encode()).hexdigest()[:20]
    return "health/" + kind + "/" + digest, digest


class ProactiveMonitor:
    def __init__(self, settings: Any, store: Any, cases: Any, ha: Any, planner: Any, *, clock=time.time) -> None:
        self.settings, self.store, self.cases, self.ha, self.planner = settings, store, cases, ha, planner
        self.clock = clock
        self.enabled = settings.proactive_checks_enabled or settings.integration_reload_repair_enabled
        self.interval = max(30, int(settings.proactive_check_interval_seconds))
        self.scans = self.read_failures = self.observations = self.recovered = 0
        self.last_scan_at = None
        self.last_result = "disabled" if not self.enabled else "waiting_for_first_scan"
        self.runtime = None

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        with database_connection(self.store.path) as db:
            db.executescript(_SCHEMA)

    def _sample(self, key: str, fault: str, now: float) -> dict[str, Any]:
        with database_connection(self.store.path) as db:
            previous = db.execute("SELECT fault,first_seen,last_seen,confirmations FROM health_observations WHERE target_key=?", (key,)).fetchone()
            first, count = now, 1
            if previous and previous[0] == fault and 0 <= now - previous[2] <= max(300, self.interval * 3):
                first, count = previous[1], previous[3]
                if now - previous[2] < self.interval * 0.8:
                    return {"first_seen": first, "confirmations": count, "fresh": False}
                count += 1
            db.execute("INSERT INTO health_observations VALUES (?,?,?,?,?) ON CONFLICT(target_key) DO UPDATE SET fault=excluded.fault,first_seen=excluded.first_seen,last_seen=excluded.last_seen,confirmations=excluded.confirmations",
                       (key, fault, first, now, count))
            return {"first_seen": first, "confirmations": count, "fresh": True}

    def _entity_fault(self, target: str, state: dict | None, now: float) -> str:
        if state is None:
            return "missing_entity"
        if state.get("entity_id") != target:
            raise ValueError("entity identity mismatch")
        if state.get("state") in {"unknown", "unavailable"}:
            return "unavailable_entity"
        if target in self.settings.proactive_stale_entities:
            raw = state.get("last_reported") or state.get("last_updated")
            timestamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.timestamp() > now + 60:
                raise ValueError("invalid state timestamp")
            if now - timestamp.timestamp() >= self.settings.proactive_stale_seconds:
                return "stale_entity"
        return "healthy"

    async def _record(self, kind: str, target: str, fault: str, sample: dict, now: float, entry: dict | None) -> None:
        pattern, fp = observation_identity(kind, target)
        if fault == "healthy":
            if sample["confirmations"] < 2:
                return
            case = await self.cases.get_case(pattern)
            if case and case.get("status") in {"new", "reopened", "diagnosed"} and not case.get("repair_plan_id"):
                await self.cases.mark_resolved(pattern)
                self.recovered += 1  # Observed recovery, never a verified AutoDoctor repair.
            return
        if not sample["fresh"] or sample["confirmations"] < 2 or now - sample["first_seen"] < self.settings.proactive_unavailable_grace_seconds:
            return
        event = LogEvent("ERROR", "native_health_probe", "", f"Enrolled {kind} {target}: {fault}",
                         "health_probe." + kind, now)
        row, new = await self.store.record(fp, event, pattern, fault)
        case, _ = await self.cases.record_event(pattern_key=pattern, pattern_label=fault, family="health_probe",
                                               fingerprint=fp, event=event, fingerprint_is_new=new)
        if case.get("status") in {"new", "reopened", "diagnosed"} and case.get("summary") != event.message and not case.get("repair_plan_id"):
            # Exact enrolled identity stays local. These cases are excluded from AI backlog triage.
            await self.cases.apply_analysis(pattern_key=pattern, fingerprint=fp,
                analysis=Analysis(event.message, "Native target observations; underlying cause is unproven.", 1.0, "medium", "monitor"),
                evidence={"origin": "native_health_observation"})
        await self.cases.publish_case(pattern)
        self.observations += 1
        if kind == "integration" and entry is not None:
            await self.planner.consider(target, entry, pattern, fp, sample["confirmations"])

    async def _check(self, kind: str, target: str) -> None:
        now = self.clock()
        pattern, _ = observation_identity(kind, target)
        try:
            entry = None
            if kind == "entity":
                state = await asyncio.wait_for(self.ha.get_state(target), 10)
                fault = self._entity_fault(target, state, now)
            else:
                entry = await asyncio.wait_for(self.ha.get_config_entry_status(target), 10)
                if entry.get("entry_id") != target:
                    raise ValueError("integration identity mismatch")
                if entry.get("disabled_by") is not None:
                    fault = "disabled_integration"  # Observation only; never re-enable an owner's disabled entry.
                elif entry.get("state") == "loaded":
                    fault = "healthy"
                elif entry.get("state") in {"setup_error", "setup_retry", "not_loaded"}:
                    fault = "integration_" + entry["state"]
                else:
                    raise ValueError("unsupported integration state")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.read_failures += 1
            self.last_result = "native_read_unavailable"
            await asyncio.to_thread(self._sample, pattern, "read_unavailable", now)
            return  # A failed read is never evidence that a target is broken or healthy.
        sample = await asyncio.to_thread(self._sample, pattern, fault, now)
        await self._record(kind, target, fault, sample, now, entry)

    def targets(self) -> list[tuple[str, str]]:
        entities = (self.settings.proactive_entities + self.settings.proactive_stale_entities) if self.settings.proactive_checks_enabled else []
        entries = list(self.settings.proactive_integration_entries) if self.settings.proactive_checks_enabled else []
        if self.settings.integration_reload_repair_enabled:
            entries += self.settings.integration_reload_targets
        # Enforce bounds in code as well as the Home Assistant settings UI.
        return ([('entity', x) for x in dict.fromkeys(entities) if isinstance(x, str) and _ENTITY.fullmatch(x)][:20]
                + [('integration', x) for x in dict.fromkeys(entries) if isinstance(x, str) and _ENTRY.fullmatch(x)][:20])

    async def run_once(self) -> None:
        if not self.enabled:
            return
        self.last_result = "scan_complete"
        for kind, target in self.targets():
            await self._check(kind, target)
            if self.runtime:
                self.runtime.beat("proactive")
        self.scans += 1
        self.last_scan_at = self.clock()

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            if self.runtime:
                self.runtime.beat("proactive")
            await asyncio.sleep(self.interval)

    def health(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "targets": len(self.targets()), "scans": self.scans,
                "last_scan_at": self.last_scan_at, "read_failures": self.read_failures,
                "observations": self.observations, "observed_recoveries": self.recovered,
                "last_result": self.last_result, "reload_recipe": self.planner.health()}
