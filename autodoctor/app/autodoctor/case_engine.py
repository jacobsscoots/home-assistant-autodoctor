from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from .ai_usage_recovery import abandon_ai_usage_before_provider, mark_ai_usage_inflight
from .case_consistency import retire_orphaned_active_case
from .cases import IncidentCaseManager
from .engine import AutoDoctorEngine
from .investigator import TargetedReadOnlyInvestigator
from .llm import NoProvider
from .models import AIResult, LogEvent
from .scheduler import incident_family

_LOG = logging.getLogger(__name__)

_REPAIR_PLANNING_GUIDANCE = """
If action=propose_fix, proposed_changes must be a list of structured proposal objects.
Each proposal should use these keys when applicable: operation, target, reason,
expected_result, rollback, preconditions. Do not invent a target identifier. If the
exact target is not present in the supplied evidence, use operation=manual_review and
explain what must be discovered first. A proposal is NOT approval and has NOT been
executed. Never instruct AutoDoctor to bypass its read-only MCP boundary.
""".strip()

_TRIAGE_CASE_STATUSES = {"new", "reopened"}


class CaseAwareAutoDoctorEngine(AutoDoctorEngine):
    """Case management, targeted diagnosis, and budgeted active-case backlog triage."""

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
        self._analysis_claim_lock = asyncio.Lock()
        self._patterns_in_analysis: set[str] = set()
        self.backlog_reconciliation: dict[str, int] = {
            "cases": 0,
            "legacy_notifications_dismissed": 0,
        }
        self.backlog_triage_runs = 0
        self.backlog_triage_analyses = 0
        self.backlog_triage_skipped = 0
        self.backlog_triage_representative_fallbacks = 0
        self.backlog_triage_orphaned_cases_retired = 0
        self.backlog_triage_last_run_at: float | None = None
        self.backlog_triage_last_error = ""

    async def initialize_case_management(self) -> dict[str, int]:
        await self.cases.initialize()
        rows = await self.store.list_recent(self.settings.max_incidents_retained)
        self.backlog_reconciliation = await self.cases.reconcile_backlog(rows)
        return dict(self.backlog_reconciliation)

    async def run_forever(self) -> None:
        triage_task: asyncio.Task[None] | None = None
        if self.settings.case_backlog_triage_enabled and not isinstance(self.llm, NoProvider):
            triage_task = asyncio.create_task(
                self._backlog_triage_loop(),
                name="autodoctor-case-backlog-triage",
            )
        try:
            await super().run_forever()
        finally:
            if triage_task is not None:
                triage_task.cancel()
                await asyncio.gather(triage_task, return_exceptions=True)

    async def _claim_pattern(self, pattern_key: str) -> bool:
        async with self._analysis_claim_lock:
            if pattern_key in self._patterns_in_analysis:
                return False
            self._patterns_in_analysis.add(pattern_key)
            return True

    async def _release_pattern(self, pattern_key: str) -> None:
        async with self._analysis_claim_lock:
            self._patterns_in_analysis.discard(pattern_key)

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
        await self._analyze_persisted_incident(
            event=event,
            fp=fp,
            family=family,
            pattern_key=pattern_key,
            pattern_label=pattern_label,
            row=row,
            source="live_event",
        )

    async def _analyze_persisted_incident(
        self,
        *,
        event: LogEvent,
        fp: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        row: dict[str, Any],
        source: str,
    ) -> bool:
        if not await self._claim_pattern(pattern_key):
            return False
        usage_id: int | None = None
        provider_started = False
        try:
            prompt = await self._prepare_prompt(
                event,
                fp=fp,
                family=family,
                pattern_key=pattern_key,
                pattern_label=pattern_label,
                row=row,
            )
            targeted = await self.investigator.collect(event, family)
            prompt += (
                "\n\nTargeted read-only MCP evidence selected deterministically by AutoDoctor "
                "(the AI did not choose these tools):\n"
                + json.dumps(targeted, ensure_ascii=False, indent=2)
                + "\n\nRepair-planning contract:\n"
                + _REPAIR_PLANNING_GUIDANCE
            )
            reservation = await self._reserve_analysis(prompt, fp, family, pattern_key)
            if reservation is None:
                return False

            usage_id, budget_reservation = reservation
            self._targeted_evidence[fp] = targeted
            self._analysis_patterns[fp] = pattern_key
            await self.cases.mark_investigating(pattern_key)
            _LOG.info(
                "Starting %s analysis for case pattern=%s fingerprint=%s",
                source,
                pattern_key,
                fp,
            )
            if not await mark_ai_usage_inflight(self.store.path, usage_id):
                raise RuntimeError("AI usage reservation could not enter inflight state; provider call blocked")
            provider_started = True
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
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            if usage_id is not None and not provider_started:
                try:
                    await abandon_ai_usage_before_provider(
                        self.store.path,
                        usage_id,
                        "Case analysis failed before external provider call began; reservation released",
                    )
                except Exception:
                    _LOG.exception("Could not release pre-provider AI reservation id=%s", usage_id)
            _LOG.exception(
                "Could not prepare or execute %s case analysis pattern=%s fingerprint=%s",
                source,
                pattern_key,
                fp,
            )
            self._targeted_evidence.pop(fp, None)
            self._analysis_patterns.pop(fp, None)
            await self.cases.mark_needs_user_action(
                pattern_key,
                "AutoDoctor could not complete case investigation safely; no repair was attempted.",
            )
            return False
        finally:
            await self._release_pattern(pattern_key)

    @staticmethod
    def _event_from_incident_row(row: dict[str, Any]) -> LogEvent:
        return LogEvent(
            level=str(row.get("level") or "ERROR"),
            name=str(row.get("name") or ""),
            source=str(row.get("source") or ""),
            message=str(row.get("message") or ""),
            exception=str(row.get("exception") or ""),
            timestamp=float(row.get("last_seen") or row.get("first_seen") or 0),
        )

    async def _eligible_backlog_cases(self) -> list[dict[str, Any]]:
        cases = await self.cases.list_cases(500)
        eligible = [case for case in cases if str(case.get("status") or "") in _TRIAGE_CASE_STATUSES]
        eligible.sort(
            key=lambda case: (
                float(case.get("last_seen") or 0),
                int(case.get("occurrences") or 0),
            ),
            reverse=True,
        )
        return eligible

    @staticmethod
    def _latest_pattern_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not rows:
            return None
        return max(
            rows,
            key=lambda row: (
                float(row.get("last_seen") or 0),
                int(row.get("occurrences") or 0),
            ),
        )

    async def _resolve_backlog_incident(
        self,
        case: dict[str, Any],
        by_fp: dict[str, dict[str, Any]],
        by_pattern: dict[str, list[dict[str, Any]]],
    ) -> tuple[str, dict[str, Any]] | None:
        pattern_key = str(case.get("pattern_key") or "")
        fp = str(case.get("representative_fingerprint") or "")
        row = by_fp.get(fp)
        if row is not None and str(row.get("pattern_key") or "") == pattern_key:
            return fp, row

        fallback = self._latest_pattern_row(by_pattern.get(pattern_key, []))
        if fallback is not None:
            self.backlog_triage_representative_fallbacks += 1
            return str(fallback.get("fingerprint") or ""), fallback

        retired = await retire_orphaned_active_case(self.store.path, pattern_key)
        if retired:
            self.backlog_triage_orphaned_cases_retired += 1
            await self.cases.publish_case(pattern_key, force=True)
        self.backlog_triage_skipped += 1
        return None

    async def _triage_backlog_case(
        self,
        case: dict[str, Any],
        by_fp: dict[str, dict[str, Any]],
        by_pattern: dict[str, list[dict[str, Any]]],
    ) -> bool:
        resolved = await self._resolve_backlog_incident(case, by_fp, by_pattern)
        if resolved is None:
            return False
        fp, row = resolved
        pattern_key = str(case.get("pattern_key") or "")

        event = self._event_from_incident_row(row)
        if not self._should_process_event(event):
            self.backlog_triage_skipped += 1
            return False

        family = incident_family(event.name, event.source)
        if not await self._should_analyze(event, row, family):
            return False

        return await self._analyze_persisted_incident(
            event=event,
            fp=fp,
            family=family,
            pattern_key=pattern_key,
            pattern_label=str(case.get("pattern_label") or row.get("pattern_label") or pattern_key),
            row=row,
            source="backlog_triage",
        )

    async def run_backlog_triage_cycle(self) -> int:
        """Analyze persisted active cases without synthesizing or re-recording log events."""
        self.backlog_triage_runs += 1
        self.backlog_triage_last_run_at = datetime.now(tz=timezone.utc).timestamp()
        if not self.settings.case_backlog_triage_enabled or isinstance(self.llm, NoProvider):
            return 0

        cases = await self._eligible_backlog_cases()
        if not cases:
            return 0
        incident_rows = await self.store.list_recent(self.settings.max_incidents_retained)
        by_fp = {str(row.get("fingerprint") or ""): row for row in incident_rows}
        by_pattern: dict[str, list[dict[str, Any]]] = {}
        for row in incident_rows:
            key = str(row.get("pattern_key") or "")
            if key:
                by_pattern.setdefault(key, []).append(row)
        completed = 0
        per_cycle = max(1, int(self.settings.case_backlog_triage_max_per_cycle))

        for case in cases:
            if completed >= per_cycle:
                break
            if await self._triage_backlog_case(case, by_fp, by_pattern):
                completed += 1
                self.backlog_triage_analyses += 1
        return completed

    async def _backlog_triage_loop(self) -> None:
        interval = max(30, int(self.settings.case_backlog_triage_interval_seconds))
        while True:
            try:
                await self.run_backlog_triage_cycle()
                self.backlog_triage_last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.backlog_triage_last_error = str(exc)[:500]
                _LOG.exception("Case backlog triage cycle failed safely")
            await asyncio.sleep(interval)

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
        case_health = await self.cases.health()
        pending = sum(
            int(count)
            for status, count in (case_health.get("cases_by_status") or {}).items()
            if status in _TRIAGE_CASE_STATUSES
        )
        case_health["notification_mode"] = "pattern-case"
        case_health["backlog_reconciliation"] = dict(self.backlog_reconciliation)
        case_health["backlog_triage"] = {
            "enabled": bool(self.settings.case_backlog_triage_enabled and not isinstance(self.llm, NoProvider)),
            "eligible_case_statuses": sorted(_TRIAGE_CASE_STATUSES),
            "pending_cases": pending,
            "interval_seconds": int(self.settings.case_backlog_triage_interval_seconds),
            "max_per_cycle": int(self.settings.case_backlog_triage_max_per_cycle),
            "runs": self.backlog_triage_runs,
            "analyses": self.backlog_triage_analyses,
            "skipped": self.backlog_triage_skipped,
            "representative_fallbacks": self.backlog_triage_representative_fallbacks,
            "orphaned_cases_retired": self.backlog_triage_orphaned_cases_retired,
            "last_run_at": self.backlog_triage_last_run_at,
            "last_error": self.backlog_triage_last_error,
            "in_flight_patterns": len(self._patterns_in_analysis),
        }
        health["case_management"] = case_health
        return health
