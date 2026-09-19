from __future__ import annotations

from aiohttp import web

from .control_dashboard import ControlDashboard


class AutomaticControlDashboard(ControlDashboard):
    """Control dashboard that reflects the opt-in automatic repair state."""

    async def index(self, request: web.Request) -> web.Response:
        response = await super().index(request)
        executor_health = await self.executor.health()
        if not executor_health.get("auto_apply_enabled"):
            return response

        text = response.text or ""
        text = text.replace(
            '<div><div class="k">Automatic repairs</div><div class="v">OFF</div></div>',
            '<div><div class="k">Automatic repairs</div><div class="v">ON — validated low-risk only</div></div>',
            1,
        )
        text = text.replace(
            '<div><div class="k">Repair executor</div><div class="v">ON — approval required</div></div>',
            '<div><div class="k">Repair executor</div><div class="v">ON — automatic low-risk + manual approval</div></div>',
            1,
        )
        safety = executor_health.get("backup_safety")
        if safety and (not safety.get("password_configured") or safety.get("uncertain_attempts")):
            text = text.replace("ON — validated low-risk only", "ARMED — blocked by backup/recovery setup")
        return web.Response(text=text, content_type="text/html")
