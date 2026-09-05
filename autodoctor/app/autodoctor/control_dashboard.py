from __future__ import annotations

import html
from typing import Any

from aiohttp import web

from .dashboard_ui import render_dashboard
from .repair_dashboard import RepairDashboard


class ControlDashboard(RepairDashboard):
    """Home Assistant ingress control surface with current safety/lifecycle state."""

    @staticmethod
    def _triage_card(health: dict[str, Any]) -> str:
        """Keep the compact triage fragment for tests and backwards-compatible callers."""

        case_health = health.get("case_management") or {}
        triage = case_health.get("backlog_triage") or {}
        statuses = case_health.get("cases_by_status") or {}
        enabled = "ON" if triage.get("enabled") else "OFF"
        last_error = str(triage.get("last_error") or "")
        error_text = html.escape(last_error[:180]) if last_error else "none"
        return (
            '<h2>Active backlog triage</h2>'
            '<div class="card grid">'
            f'<div><div class="k">Triage worker</div><div class="v">{enabled}</div></div>'
            f'<div><div class="k">Pending</div><div class="v">{int(triage.get("pending_cases", 0))}</div></div>'
            f'<div><div class="k">Investigating</div><div class="v">{int(statuses.get("investigating", 0))}</div></div>'
            f'<div><div class="k">Diagnosed</div><div class="v">{int(statuses.get("diagnosed", 0))}</div></div>'
            f'<div><div class="k">Repair ready</div><div class="v">{int(statuses.get("repair_available", 0))}</div></div>'
            f'<div><div class="k">Triage analyses</div><div class="v">{int(triage.get("analyses", 0))}</div></div>'
            f'<div><div class="k">Triage cycles</div><div class="v">{int(triage.get("runs", 0))}</div></div>'
            f'<div><div class="k">Rep fallbacks</div><div class="v">{int(triage.get("representative_fallbacks", 0))}</div></div>'
            f'<div><div class="k">Orphans retired</div><div class="v">{int(triage.get("orphaned_cases_retired", 0))}</div></div>'
            f'<div><div class="k">In flight</div><div class="v">{int(triage.get("in_flight_patterns", 0))}</div></div>'
            f'<div><div class="k">Interval</div><div class="v">{int(triage.get("interval_seconds", 0))}s</div></div>'
            f'<div><div class="k">Last error</div><div class="v">{error_text}</div></div>'
            '</div>'
            '<div class="card"><strong>Backlog triage:</strong> only new/reopened cases are considered. '
            'Persisted incident evidence is reused without manufacturing occurrences, and all existing AI budget, '
            'hourly, family and cooldown gates still apply.</div>'
        )

    async def index(self, request: web.Request) -> web.Response:
        health = await self.engine.health()
        incidents = await self.store.list_recent(50)
        cases = await self.engine.cases.list_cases(200)
        plans = await self.engine.cases.list_repair_plans(100)
        executor_health = await self.executor.health()
        body = render_dashboard(
            health=health,
            incidents=incidents,
            cases=cases,
            plans=plans,
            executor_health=executor_health,
            executor=self.executor,
            approval_nonce=self.executor.approval_nonce,
        )
        return web.Response(
            text=body,
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
