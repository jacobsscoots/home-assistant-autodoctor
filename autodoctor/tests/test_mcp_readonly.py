from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.config import Settings
from autodoctor.mcp_backend import MCPBackend, READ_ONLY_TOOLS


_MUTATING_TOOLS = {
    "call_service",
    "create_calendar_event",
    "create_recurring_calendar_event",
    "delete_calendar_events",
    "fire_event",
    "adjust_statistics",
    "clear_statistics",
    "create_automation",
    "update_automation",
    "delete_automation",
    "create_scene",
    "update_scene",
    "delete_scene",
    "create_script",
    "update_script",
    "delete_script",
    "create_helper",
    "update_helper",
    "delete_helper",
    "save_config_file",
    "delete_config_file",
    "batch_edit_config_files",
    "backup_config_files",
    "restore_config_backup",
    "cleanup_config_backups",
    "patch_dashboard_config",
    "save_dashboard_config",
    "delete_dashboard_config",
    "create_dashboard",
    "update_dashboard",
    "delete_dashboard",
    "restart_ha",
    "knx_create_entity",
    "knx_update_entity",
    "knx_delete_entity",
}


class FakeHTTPClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeResult:
    def __init__(self, *, structured=None, text: str | None = None, is_error: bool = False) -> None:
        self.structured_content = structured
        self.is_error = is_error
        self.content = [] if text is None else [SimpleNamespace(text=text)]


class FakeClient:
    def __init__(self, tools: set[str], results: dict[str, FakeResult] | None = None) -> None:
        self.tools = set(tools)
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in sorted(self.tools)])

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        return self.results.get(name, FakeResult(structured={"tool": name, "ok": True}))


def backend(tmp_path: Path, *, enabled: bool = True) -> MCPBackend:
    return MCPBackend(
        Settings(
            mcp_enabled=enabled,
            mcp_url="http://homeassistant.local:8123/api/mcp_http",
            mcp_token="super-secret-token",
        ),
        audit_path=str(tmp_path / "mcp-audit.log"),
    )


def read_audit(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_mutating_and_unknown_tools_are_not_allowlisted() -> None:
    assert not (_MUTATING_TOOLS & READ_ONLY_TOOLS)
    assert "totally_new_future_tool" not in READ_ONLY_TOOLS
    assert {"get_state", "get_system_status", "list_integrations"} <= READ_ONLY_TOOLS


def test_denied_tool_fails_before_network_and_is_audited(tmp_path: Path) -> None:
    async def run() -> None:
        value = backend(tmp_path)
        network_called = False

        def forbidden_session():
            nonlocal network_called
            network_called = True
            raise AssertionError("network must not be touched for denied tools")

        value._session = forbidden_session  # type: ignore[method-assign]
        with pytest.raises(PermissionError, match="not allowed"):
            await value.call_readonly(
                "restart_ha",
                {"confirm": True, "token": "must-not-be-logged"},
                purpose="negative safety test",
            )
        assert not network_called

    asyncio.run(run())
    records = read_audit(tmp_path / "mcp-audit.log")
    assert records[-1]["tool"] == "restart_ha"
    assert records[-1]["allowed"] is False
    assert records[-1]["success"] is False
    serialized = json.dumps(records)
    assert "must-not-be-logged" not in serialized
    assert "super-secret-token" not in serialized


def test_allowed_tool_sanitizes_and_bounds_result(tmp_path: Path) -> None:
    async def run() -> None:
        value = backend(tmp_path)
        fake = FakeClient(
            {"get_state"},
            {
                "get_state": FakeResult(
                    structured={
                        "entity_id": "sensor.private_phone",
                        "address": "192.168.1.55",
                        "token": "raw-secret-value",
                        "state": "unavailable",
                    }
                )
            },
        )
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        result = await value.call_readonly(
            "get_state",
            {"entity_id": "sensor.private_phone"},
            purpose="test state read",
        )
        assert result["entity_id"] == "<ENTITY>"
        assert result["address"] == "<IP>"
        assert result["token"] == "<REDACTED>"
        assert result["state"] == "unavailable"
        assert fake.calls == [("get_state", {"entity_id": "sensor.private_phone"})]

    asyncio.run(run())
    records = read_audit(tmp_path / "mcp-audit.log")
    assert records[-1]["allowed"] is True
    assert records[-1]["success"] is True


def test_server_reported_tool_error_fails_closed_and_is_audited(tmp_path: Path) -> None:
    async def run() -> None:
        value = backend(tmp_path)
        fake = FakeClient(
            {"get_system_status"},
            {"get_system_status": FakeResult(text="sensitive failure", is_error=True)},
        )
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="reported tool failure"):
            await value.call_readonly("get_system_status")

    asyncio.run(run())
    records = read_audit(tmp_path / "mcp-audit.log")
    assert records[-1]["allowed"] is True
    assert records[-1]["success"] is False


def test_refresh_calls_only_automatic_read_context_tools(tmp_path: Path) -> None:
    async def run() -> None:
        value = backend(tmp_path)
        exposed = {
            "get_config",
            "get_system_status",
            "list_integrations",
            "restart_ha",
            "call_service",
            "save_config_file",
        }
        fake = FakeClient(
            exposed,
            {
                "get_config": FakeResult(structured={"version": "2026.9.0"}),
                "get_system_status": FakeResult(
                    structured={"problem_entities": ["sensor.private_phone"]}
                ),
                "list_integrations": FakeResult(
                    structured=[{"domain": "tplink", "status": "loaded"}]
                ),
            },
        )
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        assert await value.refresh_context()
        assert [name for name, _ in fake.calls] == [
            "get_config",
            "get_system_status",
            "list_integrations",
        ]
        cached = value.get_relevant_config()
        assert cached["mode"] == "read-only"
        assert cached["diagnostic_context"]["get_system_status"]["problem_entities"] == [
            "<ENTITY>"
        ]
        health = await value.health()
        assert health["connected"] is True
        assert health["mode"] == "read-only"
        assert health["blocked_server_tools_count"] == 3
        assert "restart_ha" in health["blocked_server_tools_sample"]

    asyncio.run(run())


def test_missing_allowlisted_tool_fails_closed(tmp_path: Path) -> None:
    async def run() -> None:
        value = backend(tmp_path)
        fake = FakeClient({"get_config"})
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="does not expose"):
            await value.call_readonly("get_state", {"entity_id": "sensor.foo"})

    asyncio.run(run())


def test_disabled_mcp_never_opens_session(tmp_path: Path) -> None:
    async def run() -> None:
        value = backend(tmp_path, enabled=False)
        value._session = lambda: (_ for _ in ()).throw(AssertionError("network touched"))  # type: ignore[method-assign]
        await value.start()
        assert value.get_relevant_config() == {}
        health = await value.health()
        assert health == {
            "enabled": False,
            "connected": False,
            "mode": "read-only",
            "auto_context_tools": ["get_config", "get_system_status", "list_integrations"],
        }
        await value.close()

    asyncio.run(run())


def test_mcp_url_rejects_credentials_query_and_non_http_schemes(tmp_path: Path) -> None:
    value = backend(tmp_path)
    assert value._validate_url("http://ha.local/api/mcp") == "http://ha.local/api/mcp"
    for bad in (
        "ftp://ha.local/api/mcp",
        "http://user:pass@ha.local/api/mcp",
        "http://ha.local/api/mcp?token=secret",
        "http://ha.local/api/mcp#fragment",
        "not-a-url",
    ):
        with pytest.raises(RuntimeError):
            value._validate_url(bad)
