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

# Explicitly allow only diagnostic reads. Anything not listed here is denied before a
# network call is made, even if the upstream MCP server exposes it.
READ_ONLY_TOOLS = frozenset(
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

# v0.2.0 intentionally keeps automatic enrichment tiny. More targeted read-only
# enrichment can be added later without widening the model's authority.
_AUTO_CONTEXT_TOOLS = ("get_config", "get_system_status", "list_integrations")
_REFRESH_SECONDS = 300
_MAX_TOOL_PAYLOAD_CHARS = 6000
_MAX_STRING_CHARS = 1000
_MAX_LIST_ITEMS = 30
_MAX_DICT_ITEMS = 80
_MAX_DEPTH = 5
_ENTITY = re.compile(r"\b[a-z_]+\.\w+\b")
_SENSITIVE_KEY = re.compile(r"(?i)(?:api[_-]?key|token|secret|password|authorization|credential)")


class MCPBackend:
    """Fail-closed, read-only client for ganhammar/hass-mcp-server.

    The model never receives a generic MCP tool interface. AutoDoctor invokes only
    deterministic reads named in READ_ONLY_TOOLS, sanitizes and bounds every result,
    and exposes only a cached diagnostic snapshot to the prompt. Unknown or mutating
    tool names are rejected locally before any request leaves the add-on.
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
            "error": redact(str(error))[:1000],
        }
        self._audit_logger.info(json.dumps(payload, separators=(",", ":")))

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("mcp_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("mcp_url must not contain credentials, query parameters, or fragments")
        return url

    def _session(self):
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        url = self._validate_url(self.url)
        http_client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.token}"},
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
        if isinstance(value, list):
            return [cls._sanitize_value(item, depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if isinstance(value, tuple):
            return [cls._sanitize_value(item, depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for index, (raw_key, raw_value) in enumerate(value.items()):
                if index >= _MAX_DICT_ITEMS:
                    break
                key = cls._sanitize_string(str(raw_key))
                if _SENSITIVE_KEY.search(str(raw_key)):
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
        return {
            "truncated": True,
            "preview": serialized[:_MAX_TOOL_PAYLOAD_CHARS],
        }

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
        if len(blocks) == 1:
            return blocks[0]
        return blocks

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
        try:
            result = await client.call_tool(tool, arguments or {})
            decoded = self._decode_tool_result(result)
            safe = self._bound_payload(decoded)
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
            correlation_id = uuid4().hex
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
        if not self.enabled:
            raise RuntimeError("MCP is disabled")
        if not self.url or not self.token:
            raise RuntimeError("mcp_url/mcp_token missing")
        http_client, client = self._session()
        async with http_client:
            async with client:
                tools = await client.list_tools()
                self._server_tools = {str(item.name) for item in tools.tools}
                self._connected = True
                return await self._call_with_client(
                    client,
                    tool,
                    arguments,
                    purpose=purpose,
                )

    async def _refresh_once(self) -> None:
        if not self.enabled:
            return
        if not self.url or not self.token:
            raise RuntimeError("mcp_url/mcp_token missing")
        http_client, client = self._session()
        async with http_client:
            async with client:
                tools = await client.list_tools()
                self._server_tools = {str(item.name) for item in tools.tools}
                self._connected = True
                refreshed: dict[str, Any] = {}
                for tool in _AUTO_CONTEXT_TOOLS:
                    if tool not in self._server_tools:
                        continue
                    try:
                        refreshed[tool] = await self._call_with_client(
                            client,
                            tool,
                            {},
                            purpose="automatic bounded diagnostic context",
                        )
                    except Exception as exc:
                        _LOG.warning("Read-only MCP enrichment %s failed: %s", tool, redact(str(exc)))
                if refreshed:
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
            self._last_error = redact(str(exc))[:1000]
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
                self._refresh_loop(),
                name="autodoctor-readonly-mcp-refresh",
            )

    async def close(self) -> None:
        task = self._refresh_task
        self._refresh_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "connected": False,
                "mode": "read-only",
                "auto_context_tools": list(_AUTO_CONTEXT_TOOLS),
            }
        allowed_available = sorted(self._server_tools & READ_ONLY_TOOLS)
        blocked_exposed = sorted(self._server_tools - READ_ONLY_TOOLS)
        return {
            "enabled": True,
            "connected": bool(self._connected),
            "mode": "read-only",
            "last_refresh_at": self._last_refresh_at or None,
            "last_refresh_attempt_at": self._last_refresh_attempt_at or None,
            "cached_context_tools": sorted(self._context_cache),
            "allowlisted_tools_available": allowed_available,
            "blocked_server_tools_count": len(blocked_exposed),
            "blocked_server_tools_sample": blocked_exposed[:12],
            "auto_context_tools": list(_AUTO_CONTEXT_TOOLS),
            "audit_log": str(self._audit_path),
            "error": self._last_error or None,
        }

    def get_relevant_config(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        return {
            "mode": "read-only",
            "last_refresh_at": self._last_refresh_at or None,
            "diagnostic_context": dict(self._context_cache),
        }
