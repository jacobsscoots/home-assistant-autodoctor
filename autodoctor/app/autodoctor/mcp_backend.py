from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .config import Settings
from .redact import redact

_LOG = logging.getLogger(__name__)

_MISSING_AUTH_ERROR = "MCP URL/authentication missing"

_LEGACY_READ_ONLY_TOOLS = frozenset(
    {
        "get_state",
        "batch_get_state",
        "list_entities",
        "search_entities",
        "get_device_details",
        "list_calendar_events",
        "get_history",
        "get_logbook",
        "get_statistics",
        "list_statistic_ids",
        "validate_statistics",
        "list_automations",
        "get_automation_config",
        "list_scenes",
        "get_scene_config",
        "list_scripts",
        "get_script_config",
        "list_traces",
        "get_trace",
        "list_helpers",
        "get_helper_config",
        "get_config",
        "get_system_status",
        "get_domain_stats",
        "get_error_log",
        "list_areas",
        "list_devices",
        "list_services",
        "describe_service",
        "list_integrations",
        "list_labels",
    }
)

_HA_MCP_READ_ONLY_TOOLS = frozenset(
    {
        "ha_get_overview",
        "ha_get_state",
        "ha_search",
        "ha_get_system_health",
        "ha_get_integration",
        "ha_get_history",
        "ha_get_logs",
        "ha_get_automation_traces",
        "ha_get_device",
        "ha_get_entity",
        "ha_list_services",
    }
)

# Public for tests and policy inspection. This is an explicit union, not a dynamic
# server-advertised allowlist: unknown future tools remain denied by default.
READ_ONLY_TOOLS = _LEGACY_READ_ONLY_TOOLS | _HA_MCP_READ_ONLY_TOOLS

_AUTO_CONTEXT_BY_PROFILE: dict[str, tuple[tuple[str, dict[str, Any]], ...]] = {
    # The existing homeassistant-ai/ha-mcp add-on exposes a stable mandatory overview
    # reader. Keep the automatic request minimal to avoid unnecessary entity/config data.
    "ha-mcp": (("ha_get_overview", {"detail_level": "minimal"}),),
    # Backwards-compatible profile for ganhammar/hass-mcp-server style tool names.
    "ganhammar": (("get_system_status", {}), ("list_integrations", {})),
}

_REFRESH_SECONDS = 300
_MAX_TOOL_PAYLOAD_CHARS = 6000
_MAX_STRING_CHARS = 1000
_MAX_LIST_ITEMS = 30
_MAX_DICT_ITEMS = 80
_MAX_DEPTH = 5
_ENTITY = re.compile(r"\b[a-z_]+\.\w+\b")
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|authorization|credential|"
    r"latitude|longitude|location(?:_name)?|friendly_name|area(?:_name)?|floor|zone)"
)


class MCPBackend:
    """Fail-closed, read-only MCP client with explicit server-profile support.

    v0.2.1 supports both the existing homeassistant-ai/ha-mcp Home Assistant add-on
    (``ha_*`` tools + secret-path authentication) and the earlier
    ganhammar/hass-mcp-server style (legacy tool names + bearer token).

    The model never receives a generic MCP tool interface. AutoDoctor invokes only
    deterministic reads named in READ_ONLY_TOOLS, sanitizes/bounds results and exposes
    only cached diagnostic context. Unknown or write-capable tools are rejected locally.
    For the ha-mcp profile, AutoDoctor additionally requires the server to advertise
    ``readOnlyHint=True`` for every tool before it can be called.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        audit_path: str = "/data/mcp_audit.log",
    ) -> None:
        self.enabled = settings.mcp_enabled
        self.url = settings.mcp_url.strip()
        self.token = settings.mcp_token
        self._audit_path = audit_path
        self._audit_logger = self._build_audit_logger(audit_path)
        self._refresh_task: asyncio.Task[None] | None = None
        self._context_cache: dict[str, Any] = {}
        self._server_tools: set[str] = set()
        self._server_readonly_hints: dict[str, bool | None] = {}
        self._profile = "unknown"
        self._last_refresh_at = 0.0
        self._last_refresh_attempt_at = 0.0
        self._last_error = ""
        self._connected = False

    @staticmethod
    def _build_audit_logger(path: str) -> logging.Logger:
        logger = logging.getLogger(f"autodoctor.mcp_audit.{path}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if logger.handlers:
            return logger
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            destination,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger

    def _safe_error(self, error: str) -> str:
        safe = str(error)
        if self.token:
            safe = safe.replace(self.token, "<MCP_TOKEN>")
        if self.url:
            safe = safe.replace(self.url, "<MCP_URL>")
            try:
                path = urlparse(self.url).path
            except ValueError:
                path = ""
            if path:
                safe = safe.replace(path, "<MCP_SECRET_PATH>")
        return redact(safe)[:1000]

    def _audit(
        self,
        *,
        correlation_id: str,
        tool: str,
        allowed: bool,
        purpose: str,
        duration_ms: float,
        success: bool,
        error: str = "",
    ) -> None:
        payload = {
            "ts": time.time(),
            "correlation_id": correlation_id,
            "tool": str(tool)[:200],
            "allowed": bool(allowed),
            "purpose": str(purpose)[:300],
            "duration_ms": round(max(0.0, float(duration_ms)), 3),
            "success": bool(success),
            "error": self._safe_error(error),
        }
        self._audit_logger.info(json.dumps(payload, separators=(",", ":")))

    def _audit_local_failure(self, tool: str, purpose: str, error: str) -> None:
        self._audit(
            correlation_id=uuid4().hex,
            tool=tool,
            allowed=tool in READ_ONLY_TOOLS,
            purpose=purpose,
            duration_ms=0.0,
            success=False,
            error=error,
        )

    @staticmethod
    def _validate_url_shape(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("mcp_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError(
                "mcp_url must not contain embedded credentials, query parameters, or fragments"
            )
        return url

    def _auth_mode(self) -> str:
        if self.token:
            return "bearer"
        if not self.url:
            return "missing"
        try:
            parsed = urlparse(self._validate_url_shape(self.url))
        except RuntimeError:
            return "missing"
        first_segment = parsed.path.lstrip("/").split("/", 1)[0]
        if first_segment.startswith("private_") and len(first_segment) > len("private_"):
            return "secret-path"
        return "missing"

    def _validate_url(self, url: str) -> str:
        self._validate_url_shape(url)
        if self._auth_mode() == "missing":
            raise RuntimeError(
                "mcp authentication missing: configure a bearer mcp_token or a ha-mcp /private_* secret-path URL"
            )
        return url

    def _session(self):
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        url = self._validate_url(self.url)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        http_client = httpx2.AsyncClient(
            headers=headers,
            timeout=httpx2.Timeout(30.0, read=120.0),
            follow_redirects=True,
        )
        transport = streamable_http_client(url, http_client=http_client)
        return http_client, Client(transport)

    @staticmethod
    def _sanitize_string(value: str) -> str:
        safe = redact(value)
        safe = _ENTITY.sub("<ENTITY>", safe)
        return safe[:_MAX_STRING_CHARS]

    @classmethod
    def _sanitize_value(cls, value: Any, depth: int = 0) -> Any:
        if depth >= _MAX_DEPTH:
            return "<TRUNCATED_DEPTH>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return cls._sanitize_string(value)
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_value(item, depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for index, (raw_key, raw_value) in enumerate(value.items()):
                if index >= _MAX_DICT_ITEMS:
                    break
                key_text = str(raw_key)
                key = cls._sanitize_string(key_text)
                if _SENSITIVE_KEY.search(key_text):
                    result[key] = "<REDACTED>"
                else:
                    result[key] = cls._sanitize_value(raw_value, depth + 1)
            return result
        return cls._sanitize_string(str(value))

    @classmethod
    def _bound_payload(cls, value: Any) -> Any:
        safe = cls._sanitize_value(value)
        serialized = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= _MAX_TOOL_PAYLOAD_CHARS:
            return safe
        return {"truncated": True, "preview": serialized[:_MAX_TOOL_PAYLOAD_CHARS]}

    @staticmethod
    def _decode_tool_result(result: Any) -> Any:
        if bool(getattr(result, "is_error", False)):
            raise RuntimeError("MCP server reported tool failure")
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured
        blocks: list[Any] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text is None:
                continue
            try:
                blocks.append(json.loads(text))
            except (TypeError, json.JSONDecodeError):
                blocks.append(str(text))
        return blocks[0] if len(blocks) == 1 else blocks

    @staticmethod
    def _readonly_hint(tool: Any) -> bool | None:
        annotations = getattr(tool, "annotations", None)
        if annotations is None:
            return None
        for attr in ("readOnlyHint", "read_only_hint"):
            value = getattr(annotations, attr, None)
            if value is not None:
                return bool(value)
        if isinstance(annotations, dict):
            for key in ("readOnlyHint", "read_only_hint"):
                if key in annotations:
                    return bool(annotations[key])
        return None

    @staticmethod
    def _detect_profile(names: set[str]) -> str:
        if "ha_get_overview" in names or "ha_get_state" in names:
            return "ha-mcp"
        if names & _LEGACY_READ_ONLY_TOOLS:
            return "ganhammar"
        return "unknown"

    def _record_catalog(self, tools: Any) -> None:
        items = list(getattr(tools, "tools", None) or [])
        self._server_tools = {str(item.name) for item in items}
        self._server_readonly_hints = {
            str(item.name): self._readonly_hint(item) for item in items
        }
        self._profile = self._detect_profile(self._server_tools)
        if self._profile == "unknown":
            raise RuntimeError("unsupported MCP server tool profile")

    def _tool_allowed(self, tool: str) -> bool:
        if tool not in READ_ONLY_TOOLS:
            return False
        if self._profile == "ha-mcp":
            # Defence in depth: homeassistant-ai/ha-mcp has a first-class readOnlyHint
            # classification. Require the server to confirm it, not merely the name.
            return self._server_readonly_hints.get(tool) is True
        return self._profile == "ganhammar"

    def _auto_context_specs(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        return _AUTO_CONTEXT_BY_PROFILE.get(self._profile, ())

    async def _call_with_client(
        self,
        client: Any,
        tool: str,
        arguments: dict[str, Any] | None,
        *,
        purpose: str,
    ) -> Any:
        correlation_id = uuid4().hex
        started = time.monotonic()
        if tool not in READ_ONLY_TOOLS:
            self._audit(
                correlation_id=correlation_id,
                tool=tool,
                allowed=False,
                purpose=purpose,
                duration_ms=0.0,
                success=False,
                error="tool denied by read-only allowlist",
            )
            raise PermissionError(f"MCP tool is not allowed in read-only mode: {tool}")
        if tool not in self._server_tools:
            duration = (time.monotonic() - started) * 1000.0
            self._audit(
                correlation_id=correlation_id,
                tool=tool,
                allowed=True,
                purpose=purpose,
                duration_ms=duration,
                success=False,
                error="allowlisted tool is not exposed by the configured MCP server",
            )
            raise RuntimeError(f"Configured MCP server does not expose allowlisted tool: {tool}")
        if not self._tool_allowed(tool):
            duration = (time.monotonic() - started) * 1000.0
            self._audit(
                correlation_id=correlation_id,
                tool=tool,
                allowed=False,
                purpose=purpose,
                duration_ms=duration,
                success=False,
                error="server catalog did not confirm this tool as read-only",
            )
            raise PermissionError(f"MCP server did not confirm read-only safety for tool: {tool}")
        try:
            result = await client.call_tool(tool, arguments or {})
            safe = self._bound_payload(self._decode_tool_result(result))
            duration = (time.monotonic() - started) * 1000.0
            self._audit(
                correlation_id=correlation_id,
                tool=tool,
                allowed=True,
                purpose=purpose,
                duration_ms=duration,
                success=True,
            )
            return safe
        except Exception as exc:
            duration = (time.monotonic() - started) * 1000.0
            self._audit(
                correlation_id=correlation_id,
                tool=tool,
                allowed=True,
                purpose=purpose,
                duration_ms=duration,
                success=False,
                error=str(exc),
            )
            raise

    async def call_readonly(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        purpose: str = "diagnostic read",
    ) -> Any:
        """Invoke one explicitly allowlisted read tool and return only sanitized output."""
        if tool not in READ_ONLY_TOOLS:
            self._audit_local_failure(tool, purpose, "tool denied by read-only allowlist")
            raise PermissionError(f"MCP tool is not allowed in read-only mode: {tool}")
        if not self.enabled:
            self._audit_local_failure(tool, purpose, "MCP is disabled")
            raise RuntimeError("MCP is disabled")
        if not self.url or self._auth_mode() == "missing":
            self._audit_local_failure(tool, purpose, _MISSING_AUTH_ERROR)
            raise RuntimeError(_MISSING_AUTH_ERROR)
        try:
            http_client, client = self._session()
        except Exception as exc:
            self._audit_local_failure(tool, purpose, str(exc))
            raise
        async with http_client:
            async with client:
                self._record_catalog(await client.list_tools())
                self._connected = True
                return await self._call_with_client(client, tool, arguments, purpose=purpose)

    async def _refresh_once(self) -> None:
        if not self.enabled:
            return
        if not self.url or self._auth_mode() == "missing":
            raise RuntimeError(_MISSING_AUTH_ERROR)
        http_client, client = self._session()
        async with http_client:
            async with client:
                self._record_catalog(await client.list_tools())
                self._connected = True
                refreshed: dict[str, Any] = {}
                for tool, arguments in self._auto_context_specs():
                    if tool not in self._server_tools:
                        continue
                    try:
                        refreshed[tool] = await self._call_with_client(
                            client,
                            tool,
                            arguments,
                            purpose="automatic bounded diagnostic context",
                        )
                    except Exception as exc:
                        _LOG.warning(
                            "Read-only MCP enrichment %s failed: %s",
                            tool,
                            self._safe_error(str(exc)),
                        )
                self._context_cache = refreshed
                self._last_refresh_at = time.time()
                self._last_error = ""

    async def refresh_context(self) -> bool:
        self._last_refresh_attempt_at = time.time()
        try:
            await self._refresh_once()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected = False
            self._context_cache = {}
            self._last_error = self._safe_error(str(exc))
            _LOG.warning("Read-only MCP refresh failed: %s", self._last_error)
            return False

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(_REFRESH_SECONDS)
            await self.refresh_context()

    async def start(self) -> None:
        if not self.enabled:
            return
        await self.refresh_context()
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name="autodoctor-readonly-mcp-refresh"
            )

    async def close(self) -> None:
        task = self._refresh_task
        self._refresh_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "connected": False,
                "mode": "read-only",
                "server_profile": "unknown",
                "auth_mode": self._auth_mode(),
                "auto_context_tools": [],
            }
        allowed_available = sorted(
            tool for tool in self._server_tools if self._tool_allowed(tool)
        )
        blocked_exposed = sorted(self._server_tools - set(allowed_available))
        return {
            "enabled": True,
            "connected": bool(self._connected),
            "mode": "read-only",
            "server_profile": self._profile,
            "auth_mode": self._auth_mode(),
            "last_refresh_at": self._last_refresh_at or None,
            "last_refresh_attempt_at": self._last_refresh_attempt_at or None,
            "cached_context_tools": sorted(self._context_cache),
            "allowlisted_tools_available": allowed_available,
            "blocked_server_tools_count": len(blocked_exposed),
            "blocked_server_tools_sample": blocked_exposed[:12],
            "auto_context_tools": [tool for tool, _ in self._auto_context_specs()],
            "audit_log": str(self._audit_path),
            "error": self._last_error or None,
        }

    def get_relevant_config(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        return {
            "mode": "read-only",
            "server_profile": self._profile,
            "last_refresh_at": self._last_refresh_at or None,
            "diagnostic_context": dict(self._context_cache),
        }
