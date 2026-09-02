from __future__ import annotations

import json
from typing import Any

from .cases import IncidentCaseManager
from .engine import AutoDoctorEngine
from .investigator import TargetedReadOnlyInvestigator
from .llm import NoProvider
from .models import AIResult, LogEvent


class CaseAwareAutoDoctorEngine(AutoDoctorEngine):
    """v0.3 engine layer that adds case management without changing diagnosis policy."""

    def __init__(self, settings, store, ha, llm, mcp) -> None:
        super().__init__(settings, store, ha, llm, mcp)
        self.cases = IncidentCaseManager(
            store.path,
            ha,
            notifications_enabled=settings.notify_on_new_incident,
        )
        self.investigator = TargetedReadOnlyInvestigator(mcp)
        self._targeted_evidence: dict[str, dict[str, Any]] = {}
        self._analysis_patterns: dict[str, str] = {}
        self.backlog_reconciliation: dict[str, int] = {
            "cases": 0,
            "legacy_notifications_dismissed": 0,
        }

    async def initialize_case_management(self) -> dict[str, int]:
        await self.cases.initialize()
        rows = await self.store.list_recent(self.settings.max_incidents_retained)
        self.backlog_reconciliation = await self.cases.reconcile_backlog(rows)
        return dict(self.backlog_reconciliation)

    async def handle_event(self, event: LogEvent) -> None:
        if not self._should_process_event(event):
            return

        self.processed_events += 1
        fp, family, pattern_key, pattern_label, row, is_new = await self._record_incident(event)
        _case, is_new_case = await self.cases.record_event(
            pattern_key=pattern_key,
            pattern_label=pattern_label,
            family=family,
            fingerprint=fp,
            event=event,
            fingerprint_is_new=is_new,
        )
        await self._record_memory_feedback(fp, row, event, is_new)
        await self.cases.publish_case(pattern_key, force=is_new_case)

        if isinstance(self.llm, NoProvider):
            return
        if not await self._should_analyze(event, row, family):
            return

        await self.cases.mark_investigating(pattern_key)
        prompt = await self._prepare_prompt(
            event,
            fp=fp,
            family=family,
            pattern_key=pattern_key,
            pattern_label=pattern_label,
            row=row,
        )
        targeted = await self.investigator.collect(event, family)
        self._targeted_evidence[fp] = targeted
        self._analysis_patterns[fp] = pattern_key
        prompt += (
            "\n\nTargeted read-only MCP evidence selected deterministically by AutoDoctor "
            "(the AI did not choose these tools):\n"
            + json.dumps(targeted, ensure_ascii=False, indent=2)
        )

        reservation = await self._reserve_analysis(prompt, fp, family, pattern_key)
        if reservation is None:
            self._targeted_evidence.pop(fp, None)
            self._analysis_patterns.pop(fp, None)
            return
        usage_id, budget_reservation = reservation
        await self._execute_analysis(
            prompt,
            usage_id=usage_id,
            reservation=budget_reservation,
            fp=fp,
            family=family,
            pattern_key=pattern_key,
            pattern_label=pattern_label,
            row=row,
        )

    async def _handle_success(
        self,
        result: AIResult,
        *,
        usage_id,
        reservation,
        fp: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        row: dict[str, Any],
    ) -> None:
        await super()._handle_success(
            result,
            usage_id=usage_id,
            reservation=reservation,
            fp=fp,
            family=family,
            pattern_key=pattern_key,
            pattern_label=pattern_label,
            row=row,
        )
        evidence = self._targeted_evidence.pop(fp, {})
        self._analysis_patterns.pop(fp, None)
        await self.cases.apply_analysis(
            pattern_key=pattern_key,
            fingerprint=fp,
            analysis=result.analysis,
            evidence=evidence,
        )

    async def _handle_empty_result(self, usage_id, fp: str, family: str, reservation) -> None:
        await super()._handle_empty_result(usage_id, fp, family, reservation)
        pattern_key = self._analysis_patterns.pop(fp, "")
        self._targeted_evidence.pop(fp, None)
        if pattern_key:
            await self.cases.mark_needs_user_action(
                pattern_key,
                "AI investigation returned no usable diagnosis; case remains open.",
            )

    async def _handle_analysis_failure(
        self,
        exc: Exception,
        *,
        usage_id: int,
        fp: str,
        family: str,
        reservation,
    ) -> None:
        await super()._handle_analysis_failure(
            exc,
            usage_id=usage_id,
            fp=fp,
            family=family,
            reservation=reservation,
        )
        pattern_key = self._analysis_patterns.pop(fp, "")
        self._targeted_evidence.pop(fp, None)
        if pattern_key:
            await self.cases.mark_needs_user_action(
                pattern_key,
                "AI investigation failed safely; monitoring continues and no repair was attempted.",
            )

    async def health(self) -> dict[str, Any]:
        health = await super().health()
        health["case_management"] = await self.cases.health()
        health["case_management"]["notification_mode"] = "pattern-case"
        health["case_management"]["backlog_reconciliation"] = dict(self.backlog_reconciliation)
        return health
