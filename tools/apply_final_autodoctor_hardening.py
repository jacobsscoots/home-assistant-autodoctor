from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def load(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def save(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


updates: dict[str, str] = {}

# ---------------------------------------------------------------------------
# App permissions/options: use only the HA API permission actually required and
# expose a bounded incident-retention limit.
# ---------------------------------------------------------------------------
path = "autodoctor/config.yaml"
text = load(path)
text = replace_once(
    text,
    "homeassistant_api: true\nhassio_api: true\nhassio_role: homeassistant\ningress: true",
    "homeassistant_api: true\ningress: true",
    "drop unused Supervisor API permission",
)
text = replace_once(
    text,
    '  min_occurrences_for_ai: 2\n  analysis_cooldown_seconds: 900',
    '  min_occurrences_for_ai: 2\n  max_incidents_retained: 5000\n  analysis_cooldown_seconds: 900',
    "incident retention option",
)
text = replace_once(
    text,
    '  min_occurrences_for_ai: "int(1,20)"\n  analysis_cooldown_seconds: "int(60,86400)"',
    '  min_occurrences_for_ai: "int(1,20)"\n  max_incidents_retained: "int(100,50000)"\n  analysis_cooldown_seconds: "int(60,86400)"',
    "incident retention schema",
)
updates[path] = text

path = "autodoctor/app/autodoctor/config.py"
text = load(path)
text = replace_once(
    text,
    '    min_occurrences_for_ai: int = 2\n    analysis_cooldown_seconds: int = 900',
    '    min_occurrences_for_ai: int = 2\n    max_incidents_retained: int = 5000\n    analysis_cooldown_seconds: int = 900',
    "Settings incident retention",
)
updates[path] = text

path = "autodoctor/app/autodoctor/seed_store.py"
text = load(path)
text = replace_once(
    text,
    '    def __init__(self, path: str, *, seed_enabled: bool = False) -> None:\n        super().__init__(path)\n        self.seed_enabled = bool(seed_enabled)',
    '    def __init__(\n        self,\n        path: str,\n        *,\n        seed_enabled: bool = False,\n        max_incidents_retained: int = 5000,\n    ) -> None:\n        super().__init__(path, max_incidents_retained=max_incidents_retained)\n        self.seed_enabled = bool(seed_enabled)',
    "SeedAwareIncidentStore retention forwarding",
)
updates[path] = text

path = "autodoctor/app/main.py"
text = load(path)
text = replace_once(
    text,
    '        seed_enabled=settings.memory_seed_enabled,\n    )',
    '        seed_enabled=settings.memory_seed_enabled,\n        max_incidents_retained=settings.max_incidents_retained,\n    )',
    "main retention wiring",
)
updates[path] = text

# ---------------------------------------------------------------------------
# Dashboard: Home Assistant ingress must be the only network caller.
# ---------------------------------------------------------------------------
path = "autodoctor/app/autodoctor/dashboard.py"
text = load(path)
text = replace_once(
    text,
    'from .store import IncidentStore\n\n\nclass Dashboard:',
    '''from .store import IncidentStore\n\n\n_INGRESS_PROXY_IP = "172.30.32.2"\n\n\ndef ingress_remote_allowed(remote: str | None) -> bool:\n    return remote == _INGRESS_PROXY_IP\n\n\n@web.middleware\nasync def ingress_only(request: web.Request, handler):\n    if not ingress_remote_allowed(request.remote):\n        raise web.HTTPForbidden(text="AutoDoctor dashboard is available through Home Assistant ingress only.")\n    return await handler(request)\n\n\nclass Dashboard:''',
    "ingress middleware",
)
text = replace_once(
    text,
    '        app = web.Application()',
    '        app = web.Application(middlewares=[ingress_only])',
    "dashboard middleware registration",
)
updates[path] = text

# ---------------------------------------------------------------------------
# HA client: bound HTTP + websocket handshake operations and remove the unused
# Supervisor API call, which also lets the app drop hassio_api entirely.
# ---------------------------------------------------------------------------
path = "autodoctor/app/autodoctor/ha.py"
text = load(path)
text = replace_once(
    text,
    '_LOG = logging.getLogger(__name__)\n\n\nclass HomeAssistantClient:',
    '''_LOG = logging.getLogger(__name__)\n\n_HA_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)\n_HA_WS_TIMEOUT = aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10)\n_HA_WS_HANDSHAKE_TIMEOUT_SECONDS = 15\n\n\nclass HomeAssistantClient:''',
    "HA timeout constants",
)
text = replace_once(
    text,
    '        self.ws_url = "ws://supervisor/core/websocket"\n        self.supervisor_base = "http://supervisor"\n        self.session = aiohttp.ClientSession(\n            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}\n        )',
    '        self.ws_url = "ws://supervisor/core/websocket"\n        self.session = aiohttp.ClientSession(\n            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},\n            timeout=_HA_HTTP_TIMEOUT,\n        )',
    "HA session timeout and Supervisor removal",
)
text = replace_once(
    text,
    '    async def _subscribe_system_log(self, ws: aiohttp.ClientWebSocketResponse) -> None:\n        hello = await ws.receive_json()',
    '    @staticmethod\n    async def _receive_handshake_json(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:\n        return await asyncio.wait_for(\n            ws.receive_json(),\n            timeout=_HA_WS_HANDSHAKE_TIMEOUT_SECONDS,\n        )\n\n    async def _subscribe_system_log(self, ws: aiohttp.ClientWebSocketResponse) -> None:\n        hello = await self._receive_handshake_json(ws)',
    "bounded websocket greeting",
)
text = replace_once(
    text,
    '        auth = await ws.receive_json()',
    '        auth = await self._receive_handshake_json(ws)',
    "bounded websocket auth response",
)
text = replace_once(
    text,
    '        ack = await ws.receive_json()',
    '        ack = await self._receive_handshake_json(ws)',
    "bounded websocket subscription ack",
)
text = replace_once(
    text,
    '                async with self.session.ws_connect(self.ws_url, heartbeat=30) as ws:',
    '                async with self.session.ws_connect(\n                    self.ws_url, heartbeat=30, timeout=_HA_WS_TIMEOUT\n                ) as ws:',
    "websocket timeout",
)
text = replace_once(
    text,
    '''\n    async def check_config(self) -> tuple[bool, dict[str, Any]]:\n        async with self.session.post(f"{self.supervisor_base}/core/check", json={}) as response:\n            body = await response.json(content_type=None)\n            return response.status < 400 and body.get("result") == "ok", body\n''',
    '\n',
    "remove unused Supervisor check_config",
)
updates[path] = text

# ---------------------------------------------------------------------------
# Context privacy: preserve useful generic states but do not send arbitrary
# named locations or free-text helper contents to an external model.
# ---------------------------------------------------------------------------
path = "autodoctor/app/autodoctor/context.py"
text = load(path)
text = replace_once(
    text,
    ')\n\n\ndef _is_supported_entity(candidate: str) -> bool:',
    ''')\n\n_LOCATION_STATE_DOMAINS = frozenset({"person", "device_tracker"})\n_FREE_TEXT_STATE_DOMAINS = frozenset({"input_text", "text"})\n_SAFE_LOCATION_STATES = frozenset({"home", "not_home", "unknown", "unavailable"})\n\n\ndef _is_supported_entity(candidate: str) -> bool:''',
    "state privacy domain constants",
)
text = replace_once(
    text,
    'def _sanitize_text(text: str, aliases: dict[str, str]) -> str:\n    aliased = _ENTITY_CANDIDATE.sub(lambda match: _alias_match(match, aliases), text)\n    return redact(aliased)\n\n\nasync def collect_context(',
    '''def _sanitize_text(text: str, aliases: dict[str, str]) -> str:\n    aliased = _ENTITY_CANDIDATE.sub(lambda match: _alias_match(match, aliases), text)\n    return redact(aliased)\n\n\ndef sanitize_state_value(entity_id: str, value: Any, aliases: dict[str, str]) -> Any:\n    if value is None:\n        return None\n    domain = entity_id.partition(".")[0]\n    raw = str(value)\n    if domain in _FREE_TEXT_STATE_DOMAINS:\n        return "<REDACTED_TEXT_STATE>"\n    if domain in _LOCATION_STATE_DOMAINS and raw.lower() not in _SAFE_LOCATION_STATES:\n        return "<REDACTED_LOCATION_STATE>"\n    return _sanitize_text(raw, aliases)[:256]\n\n\nasync def collect_context(''',
    "state sanitization helper",
)
text = replace_once(
    text,
    '                "state": state.get("state"),',
    '                "state": sanitize_state_value(entity_id, state.get("state"), aliases),',
    "state sanitization use",
)
updates[path] = text

# ---------------------------------------------------------------------------
# Store: cap retained incidents, make open-count O(1) SQL work rather than
# loading 1,000 records, and ensure budget-blocked reservations do not consume
# hourly provider-attempt caps.
# ---------------------------------------------------------------------------
path = "autodoctor/app/autodoctor/store.py"
text = load(path)
text = replace_once(
    text,
    'class IncidentStore:\n    def __init__(self, path: str) -> None:\n        self.path = path\n        self._lock = asyncio.Lock()\n        self.fts_available = False',
    '''class IncidentStore:\n    def __init__(self, path: str, *, max_incidents_retained: int = 5000) -> None:\n        self.path = path\n        self.max_incidents_retained = max(1, int(max_incidents_retained))\n        self._lock = asyncio.Lock()\n        self.fts_available = False''',
    "IncidentStore retention constructor",
)
text = replace_once(
    text,
    '            self._ensure_fts(db)\n            self._seed_knowledge(db)\n            db.commit()',
    '            self._ensure_fts(db)\n            self._seed_knowledge(db)\n            self._prune_incidents_sync(db)\n            db.commit()',
    "startup incident pruning",
)
record_anchor = '''    async def record(\n        self,\n        fp: str,'''
prune_block = '''    def _prune_incidents_sync(\n        self,\n        db: sqlite3.Connection,\n        keep_fingerprint: str | None = None,\n    ) -> None:\n        total = int(db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0])\n        excess = total - self.max_incidents_retained\n        if excess <= 0:\n            return\n        if keep_fingerprint:\n            rows = db.execute(\n                """SELECT fingerprint FROM incidents\n                WHERE fingerprint != ? ORDER BY last_seen ASC LIMIT ?""",\n                (keep_fingerprint, excess),\n            ).fetchall()\n        else:\n            rows = db.execute(\n                "SELECT fingerprint FROM incidents ORDER BY last_seen ASC LIMIT ?",\n                (excess,),\n            ).fetchall()\n        if rows:\n            db.executemany("DELETE FROM incidents WHERE fingerprint = ?", rows)\n\n\n    async def record(\n        self,\n        fp: str,'''
text = replace_once(text, record_anchor, prune_block, "incident pruning helper")
text = replace_once(
    text,
    '            db.commit()\n            row = db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (fp,)).fetchone()\n            return dict(row), is_new',
    '            self._prune_incidents_sync(db, keep_fingerprint=fp)\n            db.commit()\n            row = db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (fp,)).fetchone()\n            return dict(row), is_new',
    "record incident pruning",
)
text = replace_once(
    text,
    '    async def ai_count_since(self, since_ts: float) -> int:\n        async with self._lock:\n            return await asyncio.to_thread(self._ai_count_since_sync, since_ts)',
    '''    async def open_incident_count(self) -> int:\n        async with self._lock:\n            return await asyncio.to_thread(self._open_incident_count_sync)\n\n    def _open_incident_count_sync(self) -> int:\n        with sqlite3.connect(self.path) as db:\n            return int(\n                db.execute(\n                    "SELECT COUNT(*) FROM incidents WHERE status IN ('open','reopened')"\n                ).fetchone()[0]\n            )\n\n    async def ai_count_since(self, since_ts: float) -> int:\n        async with self._lock:\n            return await asyncio.to_thread(self._ai_count_since_sync, since_ts)''',
    "open incident count helper",
)
text = replace_once(
    text,
    '            return int(db.execute("SELECT COUNT(*) FROM ai_usage WHERE ts >= ?", (since_ts,)).fetchone()[0])',
    '''            return int(\n                db.execute(\n                    """SELECT COUNT(*) FROM ai_usage\n                    WHERE ts >= ? AND status IN ('reserved','succeeded','failed','legacy_success')""",\n                    (since_ts,),\n                ).fetchone()[0]\n            )''',
    "global provider attempt count excludes budget blocks",
)
text = replace_once(
    text,
    '                db.execute("SELECT COUNT(*) FROM ai_usage WHERE family = ? AND ts >= ?", (str(family), float(since_ts))).fetchone()[0]\n            )',
    '''                db.execute(\n                    """SELECT COUNT(*) FROM ai_usage\n                    WHERE family = ? AND ts >= ?\n                      AND status IN ('reserved','succeeded','failed','legacy_success')""",\n                    (str(family), float(since_ts)),\n                ).fetchone()[0]\n            )''',
    "family provider attempt count excludes budget blocks",
)
text = replace_once(
    text,
    '''                """SELECT family, COUNT(*) FROM ai_usage\n                WHERE ts >= ? AND family != '' GROUP BY family\n                ORDER BY COUNT(*) DESC, family ASC LIMIT 20""",''',
    '''                """SELECT family, COUNT(*) FROM ai_usage\n                WHERE ts >= ? AND family != ''\n                  AND status IN ('reserved','succeeded','failed','legacy_success')\n                GROUP BY family\n                ORDER BY COUNT(*) DESC, family ASC LIMIT 20""",''',
    "family telemetry excludes budget blocks",
)
updates[path] = text

path = "autodoctor/app/autodoctor/engine.py"
text = load(path)
text = replace_once(
    text,
    '        incidents = await self.store.list_recent(1000)\n        mcp = await self.mcp.health()',
    '        open_incidents = await self.store.open_incident_count()\n        mcp = await self.mcp.health()',
    "cheap health incident count",
)
text = replace_once(
    text,
    '            "open_incidents": sum(1 for item in incidents if item["status"] in {"open", "reopened"}),',
    '            "open_incidents": open_incidents,\n            "incident_retention_limit": int(self.settings.max_incidents_retained),',
    "health retention metadata",
)
updates[path] = text

# ---------------------------------------------------------------------------
# UI translations: accurately describe the new retention and scheduler rules.
# ---------------------------------------------------------------------------
path = "autodoctor/translations/en.yaml"
text = load(path)
text = replace_once(
    text,
    '  min_occurrences_for_ai:\n    name: Occurrences before AI analysis\n    description: Repeated non-critical incidents must reach this count before an AI call.\n  analysis_cooldown_seconds:',
    '  min_occurrences_for_ai:\n    name: Occurrences before AI analysis\n    description: Repeated non-critical incidents must reach this count before an AI call.\n  max_incidents_retained:\n    name: Maximum retained incidents\n    description: Caps distinct incident fingerprints retained in the local database; the oldest are removed first.\n  analysis_cooldown_seconds:',
    "retention translation",
)
text = replace_once(
    text,
    '    description: Global attempt-rate protection, including budget-blocked attempts, independent from the monthly budget.',
    '    description: Global provider-attempt protection. Budget-blocked reservations are tracked separately and do not consume this hourly allowance.',
    "global scheduler translation",
)
text = replace_once(
    text,
    '  memory_enabled:\n    name: Enable local AutoDoctor memory\n    description: Retrieves bounded, privacy-safe historical knowledge and observed topology from the local SQLite database before AI diagnosis.\n  memory_max_items:',
    '  memory_enabled:\n    name: Enable local AutoDoctor memory\n    description: Retrieves bounded, privacy-safe historical knowledge and observed topology from the local SQLite database before AI diagnosis.\n  memory_seed_enabled:\n    name: Install bundled example memory\n    description: Opt-in only. Fresh databases do not install the repository bundled seed knowledge unless this is enabled.\n  memory_max_items:',
    "seed translation",
)
updates[path] = text

# ---------------------------------------------------------------------------
# Focused regression coverage for every new hardening boundary.
# ---------------------------------------------------------------------------
updates["autodoctor/tests/test_hardening.py"] = '''from __future__ import annotations\n\nimport asyncio\nimport sys\nfrom pathlib import Path\n\nimport yaml\n\nROOT = Path(__file__).resolve().parents[1]\nAPP = ROOT / "app"\nsys.path.insert(0, str(APP))\n\nfrom autodoctor.context import sanitize_state_value\nfrom autodoctor.dashboard import ingress_remote_allowed\nfrom autodoctor.ha import (\n    _HA_HTTP_TIMEOUT,\n    _HA_WS_HANDSHAKE_TIMEOUT_SECONDS,\n    _HA_WS_TIMEOUT,\n)\nfrom autodoctor.models import LogEvent\nfrom autodoctor.store import IncidentStore\n\n\ndef test_ingress_accepts_only_supervisor_proxy() -> None:\n    assert ingress_remote_allowed("172.30.32.2")\n    assert not ingress_remote_allowed("127.0.0.1")\n    assert not ingress_remote_allowed("172.30.32.3")\n    assert not ingress_remote_allowed(None)\n\n\ndef test_home_assistant_network_operations_are_bounded() -> None:\n    assert _HA_HTTP_TIMEOUT.total == 30\n    assert _HA_HTTP_TIMEOUT.connect == 10\n    assert _HA_HTTP_TIMEOUT.sock_read == 20\n    assert _HA_WS_TIMEOUT.ws_receive is None\n    assert _HA_WS_TIMEOUT.ws_close == 10\n    assert _HA_WS_HANDSHAKE_TIMEOUT_SECONDS == 15\n\n\ndef test_state_sanitizer_redacts_sensitive_state_shapes() -> None:\n    aliases: dict[str, str] = {}\n    assert sanitize_state_value("device_tracker.phone", "home", aliases) == "home"\n    assert (\n        sanitize_state_value("device_tracker.phone", "Private Workplace", aliases)\n        == "<REDACTED_LOCATION_STATE>"\n    )\n    assert sanitize_state_value("input_text.note", "personal text", aliases) == "<REDACTED_TEXT_STATE>"\n    sanitized = sanitize_state_value(\n        "sensor.status",\n        "token=secret-value user@example.com 192.168.1.5",\n        aliases,\n    )\n    assert "secret-value" not in sanitized\n    assert "user@example.com" not in sanitized\n    assert "192.168.1.5" not in sanitized\n\n\ndef test_app_does_not_request_unused_supervisor_api_permission() -> None:\n    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))\n    assert config["homeassistant_api"] is True\n    assert "hassio_api" not in config\n    assert "hassio_role" not in config\n\n\ndef test_incident_retention_and_open_count_are_bounded(tmp_path: Path) -> None:\n    async def run() -> None:\n        store = IncidentStore(str(tmp_path / "retention.db"), max_incidents_retained=3)\n        await store.initialize()\n        for index in range(5):\n            event = LogEvent(\n                level="ERROR",\n                source="test.py",\n                exception="",\n                message=f"incident {index}",\n                name="homeassistant.test",\n                timestamp=float(index + 1),\n            )\n            await store.record(f"fp-{index}", event)\n        rows = await store.list_recent(10)\n        assert [row["fingerprint"] for row in rows] == ["fp-4", "fp-3", "fp-2"]\n        assert await store.open_incident_count() == 3\n\n    asyncio.run(run())\n\n\ndef test_budget_blocks_do_not_consume_hourly_attempt_caps(tmp_path: Path) -> None:\n    async def run() -> None:\n        store = IncidentStore(str(tmp_path / "budget.db"))\n        await store.initialize()\n        blocked_id, _ = await store.reserve_ai_usage(\n            fingerprint="blocked",\n            provider="openai",\n            model="test",\n            family="family-a",\n            reserved_input_tokens=100,\n            reserved_output_tokens=100,\n            reserved_cost_usd=1.0,\n            monthly_stop_usd=0.0,\n            now_ts=1000.0,\n        )\n        assert blocked_id is None\n        assert await store.ai_count_since(999.0) == 0\n        assert await store.ai_count_for_family_since("family-a", 999.0) == 0\n        assert await store.ai_family_counts_since(999.0) == {}\n\n        usage_id, _ = await store.reserve_ai_usage(\n            fingerprint="real-attempt",\n            provider="openai",\n            model="test",\n            family="family-a",\n            reserved_input_tokens=100,\n            reserved_output_tokens=100,\n            reserved_cost_usd=0.1,\n            monthly_stop_usd=10.0,\n            now_ts=1001.0,\n        )\n        assert usage_id is not None\n        assert await store.ai_count_since(999.0) == 1\n        assert await store.ai_count_for_family_since("family-a", 999.0) == 1\n        assert await store.ai_family_counts_since(999.0) == {"family-a": 1}\n\n    asyncio.run(run())\n'''

for file_path, updated in updates.items():
    save(file_path, updated)

print(f"Applied final AutoDoctor hardening to {len(updates)} files.")
