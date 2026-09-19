"""Deterministic event-to-plan bridge for the explicitly enrolled diagnostic recipe."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .diagnostic_recipe import RECIPE_ID, RECIPE_TYPE, compile_repair, enrolled_origin, matches_incident
from .models import Analysis, LogEvent
from .repair_backup import RepairBlocked
from .repair_journal import config_digest


class DiagnosticRepairPlanner:
    def __init__(self, settings: Any, cases: Any, client: Any) -> None:
        self.settings, self.cases, self.client = settings, cases, client
        self._lock = asyncio.Lock()
        self.last_result = "no_matching_live_event"
        self.plans_created = 0

    async def consider(self, event: LogEvent, fp: str, pattern: str, row: dict[str, Any]) -> bool:
        if not matches_incident(event.message + "\n" + event.exception):
            return False
        if not self.settings.diagnostic_template_repair_enabled:
            self.last_result = "diagnostic_recipe_not_enabled"
            return False
        entity = enrolled_origin(event.name, self.settings.diagnostic_repair_entities)
        if entity is None:
            self.last_result = "diagnostic_origin_not_enrolled_or_ambiguous"
            return False
        if not 0 <= time.time() - event.timestamp <= 300 or int(row.get("occurrences", 0)) < 2:
            self.last_result = "waiting_for_recent_repeated_evidence"
            return False
        async with self._lock:
            case = await self.cases.get_case(pattern)
            if case and case.get("status") in {"repair_available", "verifying", "needs_user_action"}:
                self.last_result = "existing_plan_or_manual_attention"
                return True
            return await self._prepare(entity, fp, pattern)

    async def _prepare(self, entity: str, fp: str, pattern: str) -> bool:
        try:
            config_id, before = await self.client.resolve(entity)
            after = compile_repair(before)
        except RepairBlocked as exc:
            self.last_result = str(exc)
            return False
        except Exception:
            self.last_result = "live_diagnostic_read_failed"
            return False
        analysis = Analysis(
            summary="Compiled recipe: guard missing trigger state in an enrolled logging-only automation.",
            root_cause="Direct log-message interpolation dereferences a missing trigger state.",
            confidence=0.99, risk="low", action="propose_fix",
            checks=["fresh live configuration", "logging-only actions", "confirmed encrypted backup", "natural execution verification"],
            proposed_changes=[{
                "operation": RECIPE_TYPE, "target": config_id, "recipe_id": RECIPE_ID,
                "entity_id": entity, "before_digest": config_digest(before),
                "after_digest": config_digest(after),
            }],
        )
        # Configuration stays private. Neither original nor patched YAML enters the AI pipeline.
        plan = await self.cases.apply_analysis(
            pattern_key=pattern, fingerprint=fp, analysis=analysis,
            evidence={"origin": "compiled_diagnostic_recipe", "recipe_id": RECIPE_ID},
        )
        self.plans_created += int(plan is not None)
        self.last_result = "compiled_plan_created" if plan else "plan_not_created"
        return plan is not None

    def health(self) -> dict[str, Any]:
        return {"recipe_id": RECIPE_ID, "enabled": self.settings.diagnostic_template_repair_enabled,
                "enrolled_targets": len(self.settings.diagnostic_repair_entities),
                "plans_created": self.plans_created, "last_result": self.last_result}
