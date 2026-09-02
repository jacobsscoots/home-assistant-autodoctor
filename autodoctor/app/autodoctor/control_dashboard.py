from __future__ import annotations

import html
from typing import Any

from aiohttp import web

from .repair_dashboard import RepairDashboard


class ControlDashboard(RepairDashboard):
    """v0.4.x dashboard polish without changing any execution policy."""

    @staticmethod
    def _triage_card(health: dict[str, Any]) -> str:
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
        base = await super().index(request)
        text = base.text or ""
        text = text.replace(
            '<div class="k">Open incidents</div>',
            '<div class="k">Open incident fingerprints</div>',
            1,
        )
        old_safety = (
            '<strong>Safety:</strong> v0.2.1 supports explicit read-only MCP profiles, including the existing '
            'ha-mcp add-on. Unknown/write tools remain fail-closed and the repair executor remains disabled.'
        )
        new_safety = (
            '<strong>Safety:</strong> MCP diagnostic access is read-only and fail-closed. The repair executor, '
            'when enabled, supports only the deterministic repair allowlist and requires individual ingress '
            'approval. Automatic repairs remain disabled.'
        )
        text = text.replace(old_safety, new_safety, 1)

        health = await self.engine.health()
        triage = self._triage_card(health)
        marker = '<div class="card"><table>'
        if marker in text:
            text = text.replace(marker, triage + '<h2>Exact incident evidence</h2>' + marker, 1)
        else:
            text += triage
        return web.Response(text=text, content_type="text/html")
