from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .budget import estimate_cost_usd, reservation_for_prompt
from .config import Settings
from .context import collect_context
from .fingerprint import fingerprint
from .ha import HomeAssistantClient
from .llm import BaseProvider, NoProvider
from .mcp_backend import MCPBackend
from .models import LogEvent
from .policy import is_immediate, looks_transient, should_ignore
from .scheduler import incident_family
from .store import IncidentStore

_LOG = logging.getLogger(__name__)
_ENTITY_ID = re.compile(r"\b[a-z_]+\.[a-zA-Z0-9_]+\b")


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

    async def run_forever(self) -> None:
        async for event in self.ha.system_log_events():
            try:
                await self.handle_event(event)
            except Exception:
                _LOG.exception("Failed to process system log event")

    async def handle_event(self, event: LogEvent) -> None:
        if should_ignore(event):
            return
        if self.settings.min_level == "ERROR" and event.level != "ERROR":
            return
        self.processed_events += 1
        fp = fingerprint(event)
        family = incident_family(event.name, event.source)
        row, is_new = await self.store.record(fp, event)
        _LOG.info("Incident %s occurrence=%s %s: %s", fp, row["occurrences"], event.name, event.message[:180])

        if is_new and self.settings.notify_on_new_incident:
            await self.ha.notify(
                "AutoDoctor detected an incident",
                f"{event.name}: {event.message[:500]}\n\nFingerprint: {fp}",
                f"autodoctor_{fp}",
            )

        if isinstance(self.llm, NoProvider):
            return
        if not await self._should_analyze(event, row, family):
            return

        context = await collect_context(event, self.ha)
        entity_ids = list(dict.fromkeys(_ENTITY_ID.findall(event.message + "\n" + event.exception)))
        try:
            mcp_context = await self.mcp.get_relevant_config(entity_ids)
        except Exception as exc:
            _LOG.warning("MCP enrichment failed for %s: %s", fp, exc)
            mcp_context = {"error": str(exc)}
        context["mcp_relevant_config"] = mcp_context
        context["incident"] = {
            "fingerprint": fp,
            "occurrences": row["occurrences"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "looks_transient": looks_transient(event),
        }

        prompt = (
            "Investigate this Home Assistant incident. Do not assume missing context. "
            "If a concrete patch cannot be justified, propose checks rather than guessing.\n\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        )

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
        # Cooldown applies to every AI attempt, including provider failures and
        # local budget blocks. This prevents a repeating HA error from creating
        # a tight retry loop or a flood of budget-block records.
        await self.store.mark_analysis_attempt(fp)
        if usage_id is None:
            _LOG.warning(
                "AI budget blocked %s family=%s: spent=$%.8f reserve=$%.8f stop=$%.8f remaining=$%.8f",
                fp,
                family,
                spent_before,
                reservation.cost_usd,
                self.settings.ai_monthly_stop_usd,
                max(0.0, self.settings.ai_monthly_stop_usd - spent_before),
            )
            return

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
            max(
                0.0,
                self.settings.ai_monthly_stop_usd
                - spent_before
                - reservation.cost_usd,
            ),
        )

        try:
            result = await self.llm.analyze(prompt)
            if result is None:
                await self.store.fail_ai_usage(usage_id, "provider returned no analysis")
                usage = await self.store.monthly_ai_usage()
                _LOG.warning(
                    "AI usage failed %s family=%s reserve=$%.8f retained_spend=$%.8f remaining=$%.8f",
                    fp,
                    family,
                    reservation.cost_usd,
                    usage["spent_usd"],
                    max(0.0, self.settings.ai_monthly_stop_usd - float(usage["spent_usd"])),
                )
                return

            input_tokens = (
                result.input_tokens
                if result.input_tokens is not None
                else reservation.input_tokens
            )
            output_tokens = (
                result.output_tokens
                if result.output_tokens is not None
                else reservation.output_tokens
            )
            input_source = "provider" if result.input_tokens is not None else "reservation"
            output_source = "provider" if result.output_tokens is not None else "reservation"
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
        except Exception as exc:
            self.last_error = str(exc)
            try:
                await self.store.fail_ai_usage(usage_id, str(exc))
                usage = await self.store.monthly_ai_usage()
                _LOG.warning(
                    "AI usage failed %s family=%s reserve=$%.8f retained_spend=$%.8f remaining=$%.8f",
                    fp,
                    family,
                    reservation.cost_usd,
                    usage["spent_usd"],
                    max(0.0, self.settings.ai_monthly_stop_usd - float(usage["spent_usd"])),
                )
            except Exception:
                _LOG.exception("Failed to preserve AI budget reservation for %s", fp)
            _LOG.exception("AI analysis failed for %s", fp)

    async def _should_analyze(self, event: LogEvent, row: dict[str, Any], family: str) -> bool:
        now = datetime.now(tz=timezone.utc).timestamp()
        if row.get("last_analysis_at") and now - float(row["last_analysis_at"]) < self.settings.analysis_cooldown_seconds:
            return False
        if not is_immediate(event) and int(row["occurrences"]) < self.settings.min_occurrences_for_ai:
            return False

        # Startup backlog guard: during the grace window, old incidents remain
        # recorded/deduplicated but cannot immediately consume AI capacity.
        # Incidents first seen after this process started are still eligible, so
        # genuinely fresh failures are prioritized during startup.
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
        incidents = await self.store.list_recent(1000)
        mcp = await self.mcp.health()
        usage = await self.store.monthly_ai_usage()
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
            "max_ai_analyses_per_family_per_hour": int(
                self.settings.max_ai_analyses_per_family_per_hour
            ),
            "backlog_deferred": self.backlog_deferred,
            "family_deferred": self.family_deferred,
            "hourly_deferred": self.hourly_deferred,
            "family_attempts_last_hour": family_counts,
        }
        return {
            "status": "ok",
            "processed_events": self.processed_events,
            "open_incidents": sum(1 for x in incidents if x["status"] in {"open", "reopened"}),
            "ai_provider": self.settings.ai_provider,
            "ai_model": self.settings.ai_model,
            "ai_budget": budget,
            "ai_scheduler": scheduler,
            "mcp": mcp,
            "auto_apply_low_risk_configured": self.settings.auto_apply_low_risk,
            "auto_apply_executor_enabled": False,
            "last_error": self.last_error,
        }
