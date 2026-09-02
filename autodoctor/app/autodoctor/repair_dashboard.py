from __future__ import annotations

import html
import secrets
from typing import Any

from aiohttp import web

from .case_dashboard import CaseDashboard
from .dashboard import ingress_only


class RepairDashboard(CaseDashboard):
    """Ingress-only human approval surface for the deterministic v0.4 executor."""

    def __init__(self, settings, store, engine, executor) -> None:
        super().__init__(settings, store, engine)
        self.executor = executor

    async def start(self) -> None:
        app = web.Application(middlewares=[ingress_only])
        app.router.add_get("/", self.index)
        app.router.add_get("/api/health", self.health)
        app.router.add_get("/api/incidents", self.incidents)
        app.router.add_get("/api/cases", self.cases)
        app.router.add_get("/api/repair-plans", self.repair_plans)
        app.router.add_get("/api/repair-executor", self.repair_executor_health)
        app.router.add_post("/api/repair-plans/{plan_id}/approve", self.approve_plan)
        app.router.add_post("/api/repair-plans/{plan_id}/reject", self.reject_plan)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", 8099)
        await site.start()

    async def health(self, request: web.Request) -> web.Response:
        data = await self.engine.health()
        data["repair_executor"] = await self.executor.health()
        return web.json_response(data)

    async def repair_executor_health(self, request: web.Request) -> web.Response:
        return web.json_response(await self.executor.health())

    async def _submitted_nonce(self, request: web.Request) -> str:
        if request.content_type == "application/json":
            try:
                payload = await request.json()
            except Exception:
                return ""
            return str(payload.get("approval_nonce") or "") if isinstance(payload, dict) else ""
        data = await request.post()
        return str(data.get("approval_nonce") or "")

    async def _require_approval_nonce(self, request: web.Request) -> None:
        submitted = await self._submitted_nonce(request)
        if not submitted or not secrets.compare_digest(submitted, self.executor.approval_nonce):
            raise web.HTTPForbidden(text="Invalid or expired repair approval token.")

    @staticmethod
    def _redirect_home() -> web.Response:
        raise web.HTTPSeeOther(location="./")

    async def approve_plan(self, request: web.Request) -> web.Response:
        if not self.executor.enabled:
            raise web.HTTPForbidden(text="Repair executor is disabled.")
        await self._require_approval_nonce(request)
        plan_id = str(request.match_info.get("plan_id") or "")
        try:
            await self.executor.approve_and_execute(plan_id)
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except RuntimeError as exc:
            raise web.HTTPInternalServerError(text=str(exc)) from exc
        return self._redirect_home()

    async def reject_plan(self, request: web.Request) -> web.Response:
        await self._require_approval_nonce(request)
        plan_id = str(request.match_info.get("plan_id") or "")
        try:
            await self.executor.reject_plan(plan_id)
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return self._redirect_home()

    @staticmethod
    def _plan_summary(plan: dict[str, Any]) -> str:
        return html.escape(str(plan.get("summary") or "No summary"))

    def _plan_card(self, plan: dict[str, Any]) -> str:
        plan_id = html.escape(str(plan.get("plan_id") or ""), quote=True)
        allowed, reason, _target = self.executor.validate_plan(plan)
        status = html.escape(str(plan.get("status") or "unknown"))
        risk = html.escape(str(plan.get("risk") or "unknown"))
        confidence = float(plan.get("confidence") or 0.0)
        controls = ""
        if str(plan.get("status")) == "proposed":
            reject = (
                f'<form method="post" action="./api/repair-plans/{plan_id}/reject" style="display:inline">'
                f'<input type="hidden" name="approval_nonce" value="{html.escape(self.executor.approval_nonce, quote=True)}">'
                '<button type="submit">Reject</button></form>'
            )
            if self.executor.enabled and allowed:
                approve = (
                    f'<form method="post" action="./api/repair-plans/{plan_id}/approve" style="display:inline;margin-right:8px">'
                    f'<input type="hidden" name="approval_nonce" value="{html.escape(self.executor.approval_nonce, quote=True)}">'
                    '<button type="submit">Approve one config-entry reload</button></form>'
                )
                controls = approve + reject
            else:
                controls = reject + f'<small> Not executable: {html.escape(reason)}</small>'
        return (
            '<div class="card">'
            f'<div><strong>{self._plan_summary(plan)}</strong></div>'
            f'<div><small>Status: {status} · Risk: {risk} · Confidence: {confidence:.2f} · Type: '
            f'{html.escape(str(plan.get("repair_type") or "manual_review"))}</small></div>'
            '<div><small>Approval executes only the fixed, independently validated repair type shown above. '</n            'The AI cannot call this endpoint or choose a different Home Assistant service.</small></div>'
            f'<div style="margin-top:10px">{controls}</div>'
            '</div>'
        )

    async def index(self, request: web.Request) -> web.Response:
        base = await super().index(request)
        plans = await self.engine.cases.list_repair_plans(50)
        proposed = [plan for plan in plans if str(plan.get("status")) == "proposed"]
        executor_health = await self.executor.health()
        state = "ON — approval required" if executor_health.get("enabled") else "OFF"
        cards = "".join(self._plan_card(plan) for plan in proposed)
        if not cards:
            cards = '<div class="card">No repair plans are awaiting approval.</div>'
        section = (
            '<div class="card grid">'
            f'<div><div class="k">Repair executor</div><div class="v">{html.escape(state)}</div></div>'
            f'<div><div class="k">Supported repair types</div><div class="v">{html.escape(", ".join(executor_health.get("supported_repairs", [])))}</div></div>'
            f'<div><div class="k">Pending verification</div><div class="v">{int(executor_health.get("pending_verifications", 0))}</div></div>'
            '<div><div class="k">Automatic repairs</div><div class="v">OFF</div></div>'
            '</div>'
            '<h2>Repair plans awaiting review</h2>'
            + cards
        )
        text = base.text or ""
        marker = '<div class="card"><table>'
        if marker in text:
            text = text.replace(marker, section + marker, 1)
        else:
            text += section
        return web.Response(text=text, content_type="text/html")
