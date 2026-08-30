from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import AUTODOCTOR_VERSION
from .budget import BudgetReservation, estimate_cost_usd, reservation_for_prompt
from .config import Settings
from .context import collect_context
from .fingerprint import fingerprint
from .ha import HomeAssistantClient
from .llm import BaseProvider, NoProvider
from .mcp_backend import MCPBackend
from .memory import pattern_signature
from .models import AIResult, Analysis, LogEvent
from .policy import is_immediate, looks_transient, should_ignore
from .scheduler import incident_family
from .store import IncidentStore

_LOG = logging.getLogger(__name__)

_MEMORY_GUIDANCE = (
    "Local memory is historical evidence, not current fact. Prefer verified_fix/manually_verified "
    "over ai_hypothesis/observed. Treat expired, superseded, failed or recurring outcomes as warnings. "
    "A quiet outcome means only that no recurrence was observed in the configured window; it is not proof "
    "of a fix. Topology edges are observed relationships from incident evidence, not a complete HA config graph."
)


class AutoDoctorEngine:
    def __init__(
        self,
        settings: Settings,
        store: IncidentStore,
        ha: HomeAssistantClient,
        llm: BaseProvider,
        mcp: MCPBackend,
    ) -> None:
        self.settings = settings
        self.store = store
        self.ha = ha
        self.llm = llm
        self.mcp = mcp
        self.processed_events = 0
        self.last_error = ""
        self.started_at = datetime.now(tz=timezone.utc).timestamp()
        self.backlog_deferred = 0
        self.family_deferred = 0
        self.hourly_deferred = 0
        self.last_memory_matches = 0
        self._ha_version = "unknown"
        self._ha_version_checked = False

    async def run_forever(self) -> None:
        async for event in self.ha.system_log_events():
            try:
                await self.handle_event(event)
            except Exception:
                _LOG.exception("Failed to process system log event")

    async def _system_version(self) -> str:
        if self._ha_version_checked:
            return self._ha_version
        self._ha_version_checked = True
        try:
            self._ha_version = await self.ha.get_version()
        except Exception as exc:
            _LOG.warning("Could not read HA Core version for memory freshness: %s", exc)
            self._ha_version = "unknown"
        return self._ha_version

    def _should_process_event(self, event: LogEvent) -> bool:
        if should_ignore(event):
            return False
        return not (self.settings.min_level == "ERROR" and event.level != "ERROR")

    async def _record_incident(
        self,
        event: LogEvent,
    ) -> tuple[str, str, str, str, dict[str, Any], bool]:
        fp = fingerprint(event)
        family = incident_family(event.name, event.source)
        pattern_key, pattern_label = pattern_signature(event, family)
        row, is_new = await self.store.record(fp, event, pattern_key, pattern_label)
        _LOG.info(
            "Incident %s pattern=%s occurrence=%s %s: %s",
            fp,
            pattern_key,
            row["occurrences"],
            event.name,
            event.message[:180],
        )
        return fp, family, pattern_key, pattern_label, row, is_new

    async def _record_memory_feedback(
        self,
        fp: str,
        row: dict[str, Any],
        event: LogEvent,
        is_new: bool,
    ) -> None:
        if not self.settings.memory_enabled or is_new:
            return
        try:
            await self.store.record_recurrence_outcome(
                fp,
                int(row["occurrences"]),
                worsened_recurrences=self.settings.memory_worsened_recurrences,
                now_ts=event.timestamp,
            )
        except Exception:
            _LOG.exception("Could not record memory outcome feedback for %s", fp)

    async def _notify_new_incident(self, event: LogEvent, fp: str, is_new: bool) -> None:
        if not is_new or not self.settings.notify_on_new_incident:
            return
        await self.ha.notify(
            "AutoDoctor detected an incident",
            f"{event.name}: {event.message[:500]}\n\nFingerprint: {fp}",
            f"autodoctor_{fp}",
        )

    async def _attach_memory_context(
        self,
        context: dict[str, Any],
        *,
        fp: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
    ) -> None:
        if not self.settings.memory_enabled:
            return
        try:
            await self.store.refresh_quiet_outcomes(self.settings.memory_quiet_outcome_seconds)
            event_context = context["event"]
            query_text = " ".join(
                str(event_context.get(key) or "")
                for key in ("name", "source", "message", "exception")
            )
            memory = await self.store.retrieve_memory(
                query_text=query_text,
                family=family,
                pattern_key=pattern_key,
                pattern_label=pattern_label,
                aliases=list(context["referenced_entities"].keys()),
                limit=self.settings.memory_max_items,
                max_chars=self.settings.memory_max_chars,
            )
            self.last_memory_matches = int(memory["matches"])
            context["local_memory"] = {
                "guidance": _MEMORY_GUIDANCE,
                "knowledge": memory["knowledge"],
                "topology": memory["topology"],
                "retrieval": {
                    "matches": memory["matches"],
                    "fts5_available": memory["fts_available"],
                    "max_items": self.settings.memory_max_items,
                    "max_chars": self.settings.memory_max_chars,
                },
            }
            _LOG.info(
                "Memory retrieval %s pattern=%s family=%s matches=%d topology_edges=%d fts5=%s",
                fp,
                pattern_key,
                family,
                int(memory["matches"]),
                len(memory["topology"]),
                bool(memory["fts_available"]),
            )
        except Exception:
            self.last_memory_matches = 0
            _LOG.exception("Local memory retrieval failed for %s; continuing without RAG", fp)

    def _attach_mcp_context(self, context: dict[str, Any], fp: str) -> None:
        try:
            context["mcp_relevant_config"] = self.mcp.get_relevant_config()
        except Exception as exc:
            _LOG.warning("MCP enrichment failed for %s: %s", fp, exc)
            context["mcp_relevant_config"] = {"error": str(exc)}

    async def _prepare_prompt(
        self,
        event: LogEvent,
        *,
        fp: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        row: dict[str, Any],
    ) -> str:
        context = await collect_context(event, self.ha, self.store, family)
        context["incident"] = {
            "fingerprint": fp,
            "pattern_key": pattern_key,
            "pattern_label": pattern_label,
            "family": family,
            "occurrences": row["occurrences"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "looks_transient": looks_transient(event),
        }
        context["versions"] = {
            "home_assistant": await self._system_version(),
            "autodoctor": AUTODOCTOR_VERSION,
        }
        await self._attach_memory_context(
            context,
            fp=fp,
            family=family,
            pattern_key=pattern_key,
            pattern_label=pattern_label,
        )
        self._attach_mcp_context(context, fp)
        return (
            "Investigate this Home Assistant incident. Do not assume missing context. "
            "Use local_memory only as explicitly weighted historical evidence; do not copy an old fix blindly. "
            "If a concrete patch cannot be justified, propose checks rather than guessing.\n\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        )

    def _log_budget_block(
        self,
        fp: str,
        family: str,
        spent_before: float,
        reservation: BudgetReservation,
    ) -> None:
        _LOG.warning(
            "AI budget blocked %s family=%s: spent=$%.8f reserve=$%.8f stop=$%.8f remaining=$%.8f",
            fp,
            family,
            spent_before,
            reservation.cost_usd,
            self.settings.ai_monthly_stop_usd,
            max(0.0, self.settings.ai_monthly_stop_usd - spent_before),
        )

    def _log_reservation(
        self,
        fp: str,
        family: str,
        spent_before: float,
        reservation: BudgetReservation,
    ) -> None:
        _LOG.info(
            "AI reservation %s family=%s provider=%s model=%s input_est=%d output_max=%d "
            "reserve=$%.8f spent_before=$%.8f remaining_after_reserve=$%.8f",
            fp,
            family,
            self.llm.provider_name,
            self.llm.model,
            reservation.input_tokens,
            reservation.output_tokens,
            reservation.cost_usd,
            spent_before,
            max(0.0, self.settings.ai_monthly_stop_usd - spent_before - reservation.cost_usd),
        )

    async def _reserve_analysis(
        self,
        prompt: str,
        fp: str,
        family: str,
    ) -> tuple[int, BudgetReservation] | None:
        reservation = reservation_for_prompt(
            self.llm.reservation_input_text(prompt),
            self.llm.max_output_tokens,
            self.settings,
        )
        usage_id, spent_before = await self.store.reserve_ai_usage(
            fingerprint=fp,
            provider=self.llm.provider_name,
            model=self.llm.model,
            family=family,
            reserved_input_tokens=reservation.input_tokens,
            reserved_output_tokens=reservation.output_tokens,
            reserved_cost_usd=reservation.cost_usd,
            monthly_stop_usd=self.settings.ai_monthly_stop_usd,
        )
        await self.store.mark_analysis_attempt(fp)
        if usage_id is None:
            self._log_budget_block(fp, family, spent_before, reservation)
            return None
        self._log_reservation(fp, family, spent_before, reservation)
        return usage_id, reservation

    async def _log_failed_usage(
        self,
        fp: str,
        family: str,
        reservation: BudgetReservation,
    ) -> None:
        usage = await self.store.monthly_ai_usage()
        _LOG.warning(
            "AI usage failed %s family=%s reserve=$%.8f retained_spend=$%.8f remaining=$%.8f",
            fp,
            family,
            reservation.cost_usd,
            usage["spent_usd"],
            max(0.0, self.settings.ai_monthly_stop_usd - float(usage["spent_usd"])),
        )

    async def _handle_empty_result(
        self,
        usage_id: int,
        fp: str,
        family: str,
        reservation: BudgetReservation,
    ) -> None:
        await self.store.fail_ai_usage(usage_id, "provider returned no analysis")
        await self._log_failed_usage(fp, family, reservation)

    @staticmethod
    def _token_value(provider_value: int | None, reserved_value: int) -> tuple[int, str]:
        if provider_value is None:
            return reserved_value, "reservation"
        return provider_value, "provider"

    async def _persist_ai_memory(
        self,
        *,
        fp: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        analysis: Analysis,
        row: dict[str, Any],
    ) -> None:
        if not self.settings.memory_enabled:
            return
        try:
            await self.store.save_ai_memory(
                fingerprint=fp,
                family=family,
                pattern_key=pattern_key,
                pattern_label=pattern_label,
                analysis=analysis,
                occurrences=int(row["occurrences"]),
                ha_version=self._ha_version,
                autodoctor_version=AUTODOCTOR_VERSION,
                expiry_days=self.settings.memory_ai_hypothesis_expiry_days,
            )
        except Exception:
            _LOG.exception("Could not persist AI hypothesis memory for %s", fp)

    async def _handle_success(
        self,
        result: AIResult,
        *,
        usage_id: int,
        reservation: BudgetReservation,
        fp: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        row: dict[str, Any],
    ) -> None:
        input_tokens, input_source = self._token_value(result.input_tokens, reservation.input_tokens)
        output_tokens, output_source = self._token_value(result.output_tokens, reservation.output_tokens)
        actual_cost = estimate_cost_usd(
            input_tokens,
            output_tokens,
            self.settings.ai_input_cost_per_million_usd,
            self.settings.ai_output_cost_per_million_usd,
        )
        await self.store.finalize_ai_usage(
            usage_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=actual_cost,
        )

        analysis = result.analysis
        await self.store.save_analysis(fp, analysis)
        await self._persist_ai_memory(
            fp=fp,
            family=family,
            pattern_key=pattern_key,
            pattern_label=pattern_label,
            analysis=analysis,
            row=row,
        )

        usage = await self.store.monthly_ai_usage()
        _LOG.info(
            "AI usage %s family=%s input_tokens=%d input_source=%s output_tokens=%d output_source=%s "
            "reserved=$%.8f actual=$%.8f month_spend=$%.8f remaining=$%.8f",
            fp,
            family,
            input_tokens,
            input_source,
            output_tokens,
            output_source,
            reservation.cost_usd,
            actual_cost,
            usage["spent_usd"],
            max(0.0, self.settings.ai_monthly_stop_usd - float(usage["spent_usd"])),
        )
        _LOG.info(
            "AI analysis %s risk=%s confidence=%.2f action=%s: %s",
            fp,
            analysis.risk,
            analysis.confidence,
            analysis.action,
            analysis.summary[:220],
        )
        if self.settings.auto_apply_low_risk:
            _LOG.warning("auto_apply_low_risk requested, but v0.1 executor is intentionally disabled")

    async def _handle_analysis_failure(
        self,
        exc: Exception,
        *,
        usage_id: int,
        fp: str,
        family: str,
        reservation: BudgetReservation,
    ) -> None:
        self.last_error = str(exc)
        try:
            await self.store.fail_ai_usage(usage_id, str(exc))
            await self._log_failed_usage(fp, family, reservation)
        except Exception:
            _LOG.exception("Failed to preserve AI budget reservation for %s", fp)
        _LOG.exception("AI analysis failed for %s", fp)

    async def _execute_analysis(
        self,
        prompt: str,
        *,
        usage_id: int,
        reservation: BudgetReservation,
        fp: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        row: dict[str, Any],
    ) -> None:
        try:
            result = await self.llm.analyze(prompt)
            if result is None:
                await self._handle_empty_result(usage_id, fp, family, reservation)
                return
            await self._handle_success(
                result,
                usage_id=usage_id,
                reservation=reservation,
                fp=fp,
                family=family,
                pattern_key=pattern_key,
                pattern_label=pattern_label,
                row=row,
            )
        except Exception as exc:
            await self._handle_analysis_failure(
                exc,
                usage_id=usage_id,
                fp=fp,
                family=family,
                reservation=reservation,
            )

    async def handle_event(self, event: LogEvent) -> None:
        if not self._should_process_event(event):
            return

        self.processed_events += 1
        fp, family, pattern_key, pattern_label, row, is_new = await self._record_incident(event)
        await self._record_memory_feedback(fp, row, event, is_new)
        await self._notify_new_incident(event, fp, is_new)

        if isinstance(self.llm, NoProvider):
            return
        if not await self._should_analyze(event, row, family):
            return

        prompt = await self._prepare_prompt(
            event,
            fp=fp,
            family=family,
            pattern_key=pattern_key,
            pattern_label=pattern_label,
            row=row,
        )
        reservation = await self._reserve_analysis(prompt, fp, family)
        if reservation is None:
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

    async def _should_analyze(self, event: LogEvent, row: dict[str, Any], family: str) -> bool:
        now = datetime.now(tz=timezone.utc).timestamp()
        if row.get("last_analysis_at") and now - float(row["last_analysis_at"]) < self.settings.analysis_cooldown_seconds:
            return False
        if not is_immediate(event) and int(row["occurrences"]) < self.settings.min_occurrences_for_ai:
            return False

        grace = max(0, int(self.settings.ai_startup_backlog_grace_seconds))
        first_seen = float(row.get("first_seen") or 0)
        if grace and first_seen < self.started_at and now - self.started_at < grace:
            self.backlog_deferred += 1
            return False

        since = now - 3600
        count = await self.store.ai_count_since(since)
        if count >= self.settings.max_ai_analyses_per_hour:
            self.hourly_deferred += 1
            return False

        family_cap = max(
            1,
            min(
                int(self.settings.max_ai_analyses_per_family_per_hour),
                int(self.settings.max_ai_analyses_per_hour),
            ),
        )
        family_count = await self.store.ai_count_for_family_since(family, since)
        if family_count >= family_cap:
            self.family_deferred += 1
            return False
        return True

    async def health(self) -> dict[str, Any]:
        open_incidents = await self.store.open_incident_count()
        mcp = await self.mcp.health()
        usage = await self.store.monthly_ai_usage()
        memory = await self.store.memory_health()
        now = datetime.now(tz=timezone.utc).timestamp()
        family_counts = await self.store.ai_family_counts_since(now - 3600)
        stop = max(0.0, float(self.settings.ai_monthly_stop_usd))
        spent = float(usage["spent_usd"])
        budget = {
            "enabled": self.settings.ai_budget_enabled,
            "month_utc": usage["month_utc"],
            "monthly_budget_usd": float(self.settings.ai_monthly_budget_usd),
            "stop_threshold_usd": stop,
            "spent_usd": spent,
            "remaining_to_stop_usd": max(0.0, stop - spent),
            "analyses_count": usage["analyses_count"],
            "attempts_count": usage["attempts_count"],
            "failed_count": usage["failed_count"],
            "reserved_count": usage["reserved_count"],
            "budget_blocked_count": usage["budget_blocked_count"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "input_cost_per_million_usd": float(self.settings.ai_input_cost_per_million_usd),
            "output_cost_per_million_usd": float(self.settings.ai_output_cost_per_million_usd),
        }
        scheduler = {
            "startup_backlog_grace_seconds": int(self.settings.ai_startup_backlog_grace_seconds),
            "startup_grace_remaining_seconds": max(
                0,
                int(self.settings.ai_startup_backlog_grace_seconds - (now - self.started_at)),
            ),
            "max_ai_analyses_per_hour": int(self.settings.max_ai_analyses_per_hour),
            "max_ai_analyses_per_family_per_hour": int(self.settings.max_ai_analyses_per_family_per_hour),
            "backlog_deferred": self.backlog_deferred,
            "family_deferred": self.family_deferred,
            "hourly_deferred": self.hourly_deferred,
            "family_attempts_last_hour": family_counts,
        }
        memory.update(
            {
                "enabled": bool(self.settings.memory_enabled),
                "max_items_per_prompt": int(self.settings.memory_max_items),
                "max_chars_per_prompt": int(self.settings.memory_max_chars),
                "last_retrieval_matches": int(self.last_memory_matches),
                "ai_hypothesis_expiry_days": int(self.settings.memory_ai_hypothesis_expiry_days),
                "quiet_outcome_seconds": int(self.settings.memory_quiet_outcome_seconds),
                "worsened_recurrences": int(self.settings.memory_worsened_recurrences),
                "home_assistant_version": self._ha_version,
                "autodoctor_version": AUTODOCTOR_VERSION,
            }
        )
        return {
            "status": "ok",
            "processed_events": self.processed_events,
            "open_incidents": open_incidents,
            "incident_retention_limit": int(self.settings.max_incidents_retained),
            "ai_provider": self.settings.ai_provider,
            "ai_model": self.settings.ai_model,
            "ai_budget": budget,
            "ai_scheduler": scheduler,
            "memory": memory,
            "mcp": mcp,
            "auto_apply_low_risk_configured": self.settings.auto_apply_low_risk,
            "auto_apply_executor_enabled": False,
            "last_error": self.last_error,
        }
