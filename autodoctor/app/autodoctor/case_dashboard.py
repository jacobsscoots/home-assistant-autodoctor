from __future__ import annotations

import html
from typing import Any

from aiohttp import web

from .dashboard import Dashboard, ingress_only


class CaseDashboard(Dashboard):
    """Dashboard extension for pattern-level cases and non-executable repair plans."""

    async def start(self) -> None:
        app = web.Application(middlewares=[ingress_only])
        app.router.add_get("/", self.index)
        app.router.add_get("/api/health", self.health)
        app.router.add_get("/api/incidents", self.incidents)
        app.router.add_get("/api/cases", self.cases)
        app.router.add_get("/api/repair-plans", self.repair_plans)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", 8099)
        await site.start()

    async def cases(self, request: web.Request) -> web.Response:
        manager = getattr(self.engine, "cases", None)
        if manager is None:
            return web.json_response([])
        return web.json_response(await manager.list_cases(200))

    async def repair_plans(self, request: web.Request) -> web.Response:
        manager = getattr(self.engine, "cases", None)
        if manager is None:
            return web.json_response([])
        return web.json_response(await manager.list_repair_plans(200))

    async def index(self, request: web.Request) -> web.Response:
        base = await super().index(request)
        manager = getattr(self.engine, "cases", None)
        if manager is None:
            return base
        case_health: dict[str, Any] = await manager.health()
        statuses = case_health.get("cases_by_status", {})
        active = sum(
            int(count)
            for status, count in statuses.items()
            if status not in {"historical", "resolved"}
        )
        card = (
            '<div class="card grid">'
            f'<div><div class="k">Active cases</div><div class="v">{active}</div></div>'
            f'<div><div class="k">Historical cases</div><div class="v">{int(statuses.get("historical", 0))}</div></div>'
            f'<div><div class="k">Repair plans awaiting review</div><div class="v">{int(case_health.get("repair_plans_proposed", 0))}</div></div>'
            '<div><div class="k">Repair executor</div><div class="v">OFF</div></div>'
            '</div>'
            '<div class="card"><strong>Case management:</strong> related exact fingerprints are grouped into one notification and one repair-planning lifecycle. Repair plans are review-only in v0.3.0.</div>'
        )
        text = base.text or ""
        marker = '<div class="card"><table>'
        if marker in text:
            text = text.replace(marker, card + marker, 1)
        else:
            text += card
        return web.Response(text=text, content_type="text/html")
