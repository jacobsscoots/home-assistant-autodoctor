"""Recognize fresh events for the explicitly enrolled JSON/Base64 logging recipe."""
from __future__ import annotations

import asyncio
from collections import deque
import time
from typing import Any

from .audit_log_recipe import ORIGIN, RECIPE_ID, REPAIR_TYPE, enrolled, matches_incident, reviewed_patch
from .models import Analysis, LogEvent
from .repair_backup import RepairBlocked
from .repair_journal import config_digest


class AuditLogRepairPlanner:
    def __init__(self, settings: Any, cases: Any, client: Any) -> None:
        self.settings, self.cases, self.client = settings, cases, client
        self._lock = asyncio.Lock()
        self._events: deque[float] = deque(maxlen=2)
        self.last_result = "no_matching_live_event"
        self.plans_created = 0

    async def consider(self, event: LogEvent, fp: str, pattern: str, _row: dict[str, Any]) -> bool:
        if not event.name.startswith("homeassistant.components.script.") or not matches_incident(event.message + "\n" + event.exception):
            return False
        if not enrolled(self.settings):
            self.last_result = "audit_log_recipe_not_enrolled"
            return False
        now = time.time()
        if not 0 <= now - event.timestamp <= 300:
            self.last_result = "audit_log_event_not_recent"
            return False
        async with self._lock:
            try:
                return await self._consider(event, fp, pattern, now)
            except RepairBlocked as exc:
                self.last_result = str(exc)
            except Exception:
                self.last_result = "audit_log_live_evidence_unavailable"
            return False

    async def _consider(self, event: LogEvent, fp: str, pattern: str, now: float) -> bool:
        key, before = await self.client.resolve()
        if event.name != "homeassistant.components.script." + key:
            return False  # A caller or another logger cannot identify this repair target.
        after = reviewed_patch(self.settings, before)
        case = await self.cases.get_case(pattern)
        if not case or case.get("status") in {"repair_available", "verifying", "needs_user_action"}:
            self.last_result = "audit_log_existing_plan_or_manual_attention"
            return True
        self._events = deque((stamp for stamp in self._events if now - stamp <= 300), maxlen=2)
        if event.timestamp not in self._events:
            self._events.append(event.timestamp)
        if len(self._events) < 2:
            self.last_result = "audit_log_waiting_for_second_recent_error"
            return True
        analysis = Analysis(
            summary="Compiled logger repair: serialize JSON and encode Base64 in one template.",
            root_cause="An intermediate template variable can parse JSON into a native object before Base64 encoding.",
            confidence=0.99, risk="low", action="propose_fix",
            checks=["operator-reviewed script and append-only helper", "idle script", "confirmed encrypted backup", "natural encoding roundtrip with formerly failing input"],
            proposed_changes=[{"operation": REPAIR_TYPE, "recipe_id": RECIPE_ID, "target": key,
                               "entity_id": self.settings.audit_log_repair_entity,
                               "before_digest": config_digest(before), "after_digest": config_digest(after)}],
        )
        plan = await self.cases.apply_analysis(
            pattern_key=pattern, fingerprint=fp, analysis=analysis,
            evidence={"origin": ORIGIN, "recipe_id": RECIPE_ID},
        )
        self.plans_created += int(plan is not None)
        self.last_result = "audit_log_compiled_plan_created" if plan else "audit_log_plan_not_created"
        self._events.clear()
        return plan is not None

    def health(self) -> dict[str, Any]:
        return {"recipe_id": RECIPE_ID, "enrolled": enrolled(self.settings),
                "plans_created": self.plans_created, "last_result": self.last_result}
