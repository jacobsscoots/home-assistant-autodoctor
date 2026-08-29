from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

from aiohttp import web

from .config import Settings
from .engine import AutoDoctorEngine
from .store import IncidentStore


class Dashboard:
    def __init__(self, settings: Settings, store: IncidentStore, engine: AutoDoctorEngine) -> None:
        self.settings = settings
        self.store = store
        self.engine = engine
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/api/health", self.health)
        app.router.add_get("/api/incidents", self.incidents)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", 8099)
        await site.start()

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(await self.engine.health())

    async def incidents(self, request: web.Request) -> web.Response:
        return web.json_response(await self.store.list_recent(100))

    async def index(self, request: web.Request) -> web.Response:
        incidents = await self.store.list_recent(50)
        health = await self.engine.health()
        rows = "".join(self._row(item) for item in incidents) or '<tr><td colspan="7">No incidents captured yet.</td></tr>'
        mcp = health.get("mcp", {})
        mcp_text = "connected" if mcp.get("connected") else ("disabled" if not mcp.get("enabled") else "not connected")
        budget = health.get("ai_budget", {})
        budget_text = (
            f"${budget.get('spent_usd', 0):.4f} / ${budget.get('stop_threshold_usd', 0):.2f}"
            if budget.get("enabled")
            else "locked"
        )
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="20"><title>AutoDoctor</title>
<style>
body{{font-family:system-ui,sans-serif;margin:20px;background:#111827;color:#e5e7eb}}
.card{{background:#1f2937;border-radius:12px;padding:16px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.k{{font-size:12px;color:#9ca3af}} .v{{font-size:20px;font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:8px;border-bottom:1px solid #374151;text-align:left;vertical-align:top}}
.low{{color:#86efac}} .medium{{color:#fde047}} .high{{color:#fca5a5}} code{{color:#93c5fd}}
small{{color:#9ca3af}}
</style></head><body>
<h1>🩺 AutoDoctor</h1>
<div class="card grid">
<div><div class="k">Watcher</div><div class="v">{html.escape(health['status'])}</div></div>
<div><div class="k">Events processed</div><div class="v">{health['processed_events']}</div></div>
<div><div class="k">Open incidents</div><div class="v">{health['open_incidents']}</div></div>
<div><div class="k">AI</div><div class="v">{html.escape(health['ai_provider'])}</div></div>
<div><div class="k">AI spend / stop</div><div class="v">{html.escape(budget_text)}</div></div>
<div><div class="k">AI analyses</div><div class="v">{int(budget.get('analyses_count', 0))}</div></div>
<div><div class="k">Budget blocked</div><div class="v">{int(budget.get('budget_blocked_count', 0))}</div></div>
<div><div class="k">MCP</div><div class="v">{html.escape(mcp_text)}</div></div>
<div><div class="k">Auto apply</div><div class="v">OFF</div></div>
</div>
<div class="card"><strong>Safety:</strong> v0.1.2 can monitor and store AI diagnoses, but the repair executor remains hard-disabled.</div>
<div class="card"><table><thead><tr><th>Last seen</th><th>Count</th><th>Level</th><th>Source</th><th>Message</th><th>AI</th><th>Fingerprint</th></tr></thead><tbody>{rows}</tbody></table></div>
</body></html>"""
        return web.Response(text=body, content_type="text/html")

    def _row(self, item: dict[str, Any]) -> str:
        ts = datetime.fromtimestamp(float(item["last_seen"])).strftime("%Y-%m-%d %H:%M:%S")
        analysis = None
        if item.get("analysis_json"):
            try:
                analysis = json.loads(item["analysis_json"])
            except json.JSONDecodeError:
                pass
        ai = "not analysed"
        if analysis:
            risk = html.escape(str(analysis.get("risk", "high")))
            summary = html.escape(str(analysis.get("summary", "")))
            ai = f'<span class="{risk}">{risk}</span>: {summary}'
        return (
            "<tr>"
            f"<td>{html.escape(ts)}</td>"
            f"<td>{int(item['occurrences'])}</td>"
            f"<td>{html.escape(item['level'])}</td>"
            f"<td><small>{html.escape(item['name'])}</small></td>"
            f"<td>{html.escape(item['message'][:350])}</td>"
            f"<td>{ai}</td>"
            f"<td><code>{html.escape(item['fingerprint'])}</code></td>"
            "</tr>"
        )
