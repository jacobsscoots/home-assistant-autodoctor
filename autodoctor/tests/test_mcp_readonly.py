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

_LEGACY_MUTATING = {
    "call_service",
    "restart_ha",
    "save_config_file",
    "update_automation",
    "create_script",
    "restore_config_backup",
}
_HA_MCP_MUTATING = {
    "ha_call_service",
    "ha_call_event",
    "ha_restart",
    "ha_reload_core",
    "ha_config_set_automation",
    "ha_config_set_script",
    "ha_config_set_scene",
    "ha_set_integration",
    "ha_set_entity",
    "ha_remove_entity",
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
    def __init__(
        self,
        tools: set[str] | dict[str, bool | None],
        results: dict[str, FakeResult] | None = None,
    ) -> None:
        if isinstance(tools, dict):
            self.tool_hints = dict(tools)
        else:
            self.tool_hints = {name: None for name in tools}
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_tools(self):
        items = []
        for name, hint in sorted(self.tool_hints.items()):
            annotations = None if hint is None else SimpleNamespace(readOnlyHint=hint)
            items.append(SimpleNamespace(name=name, annotations=annotations))
        return SimpleNamespace(tools=items)

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        return self.results.get(name, FakeResult(structured={"tool": name, "ok": True}))


def legacy_backend(tmp_path: Path, *, enabled: bool = True) -> MCPBackend:
    return MCPBackend(
        Settings(
            mcp_enabled=enabled,
            mcp_url="http://homeassistant.local:8123/api/mcp_http",
            mcp_token="super-secret-token",
        ),
        audit_path=str(tmp_path / "mcp-audit.log"),
    )


def ha_mcp_backend(tmp_path: Path, *, enabled: bool = True) -> MCPBackend:
    return MCPBackend(
        Settings(
            mcp_enabled=enabled,
            mcp_url="http://127.0.0.1:9583/private_testSecretPath",
            mcp_token="",
        ),
        audit_path=str(tmp_path / "mcp-audit.log"),
    )


def read_audit(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_mutating_and_unknown_tools_are_not_allowlisted() -> None:
    assert not ((_LEGACY_MUTATING | _HA_MCP_MUTATING) & READ_ONLY_TOOLS)
    assert "totally_new_future_tool" not in READ_ONLY_TOOLS
    assert {"get_state", "get_system_status", "list_integrations"} <= READ_ONLY_TOOLS
    assert {"ha_get_state", "ha_get_overview", "ha_get_system_health"} <= READ_ONLY_TOOLS


def test_legacy_denied_tool_fails_before_network_and_is_audited(tmp_path: Path) -> None:
    async def run() -> None:
        value = legacy_backend(tmp_path)
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
    serialized = json.dumps(records)
    assert "must-not-be-logged" not in serialized
    assert "super-secret-token" not in serialized


def test_legacy_allowed_tool_sanitizes_private_data(tmp_path: Path) -> None:
    async def run() -> None:
        value = legacy_backend(tmp_path)
        fake = FakeClient(
            {"get_state"},
            {
                "get_state": FakeResult(
                    structured={
                        "entity_id": "sensor.private_phone",
                        "address": "192.168.1.55",
                        "token": "raw-secret-value",
                        "latitude": 51.12345,
                        "longitude": -1.23456,
                        "location_name": "Private Home",
                        "area_name": "Bedroom",
                        "state": "unavailable",
                    }
                )
            },
        )
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        result = await value.call_readonly(
            "get_state", {"entity_id": "sensor.private_phone"}, purpose="test state read"
        )
        assert result["entity_id"] == "<ENTITY>"
        assert result["address"] == "<IP>"
        assert result["token"] == "<REDACTED>"
        assert result["latitude"] == "<REDACTED>"
        assert result["longitude"] == "<REDACTED>"
        assert result["location_name"] == "<REDACTED>"
        assert result["area_name"] == "<REDACTED>"
        assert result["state"] == "unavailable"

    asyncio.run(run())


def test_legacy_refresh_preserves_v020_auto_context(tmp_path: Path) -> None:
    async def run() -> None:
        value = legacy_backend(tmp_path)
        fake = FakeClient(
            {"get_config", "get_system_status", "list_integrations", "restart_ha"},
            {
                "get_system_status": FakeResult(structured={"entities": 120}),
                "list_integrations": FakeResult(structured=[{"domain": "tplink"}]),
            },
        )
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        assert await value.refresh_context()
        assert fake.calls == [("get_system_status", {}), ("list_integrations", {})]
        health = await value.health()
        assert health["server_profile"] == "ganhammar"
        assert health["auth_mode"] == "bearer"
        assert health["auto_context_tools"] == ["get_system_status", "list_integrations"]
        assert "restart_ha" in health["blocked_server_tools_sample"]

    asyncio.run(run())


def test_ha_mcp_secret_path_auth_requires_no_bearer_token(tmp_path: Path) -> None:
    value = ha_mcp_backend(tmp_path)
    assert value._auth_mode() == "secret-path"
    assert value._validate_url(value.url) == value.url
    missing = MCPBackend(
        Settings(mcp_enabled=True, mcp_url="http://127.0.0.1:9583/", mcp_token=""),
        audit_path=str(tmp_path / "missing.log"),
    )
    assert missing._auth_mode() == "missing"
    with pytest.raises(RuntimeError, match="authentication missing"):
        missing._validate_url(missing.url)


def test_ha_mcp_profile_uses_only_minimal_overview_automatically(tmp_path: Path) -> None:
    async def run() -> None:
        value = ha_mcp_backend(tmp_path)
        fake = FakeClient(
            {
                "ha_get_overview": True,
                "ha_get_state": True,
                "ha_get_system_health": True,
                "ha_restart": False,
                "ha_call_service": False,
            },
            {
                "ha_get_overview": FakeResult(
                    structured={
                        "system_summary": {"total_entities": 250},
                        "example_entity": "sensor.private_phone",
                        "area_name": "Bedroom",
                    }
                )
            },
        )
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        assert await value.refresh_context()
        assert fake.calls == [("ha_get_overview", {"detail_level": "minimal"})]
        cached = value.get_relevant_config()
        assert cached["server_profile"] == "ha-mcp"
        assert cached["diagnostic_context"]["ha_get_overview"]["example_entity"] == "<ENTITY>"
        assert cached["diagnostic_context"]["ha_get_overview"]["area_name"] == "<REDACTED>"
        health = await value.health()
        assert health["connected"] is True
        assert health["server_profile"] == "ha-mcp"
        assert health["auth_mode"] == "secret-path"
        assert health["auto_context_tools"] == ["ha_get_overview"]
        assert "ha_restart" in health["blocked_server_tools_sample"]
        assert "ha_call_service" in health["blocked_server_tools_sample"]

    asyncio.run(run())


def test_ha_mcp_allowlisted_name_still_requires_readonly_annotation(tmp_path: Path) -> None:
    async def run() -> None:
        value = ha_mcp_backend(tmp_path)
        fake = FakeClient({"ha_get_overview": True, "ha_get_state": False})
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        with pytest.raises(PermissionError, match="did not confirm read-only"):
            await value.call_readonly("ha_get_state", {"entity_id": "sensor.foo"})
        assert fake.calls == []

    asyncio.run(run())
    records = read_audit(tmp_path / "mcp-audit.log")
    assert records[-1]["tool"] == "ha_get_state"
    assert records[-1]["allowed"] is False


def test_ha_mcp_write_tool_is_denied_before_network(tmp_path: Path) -> None:
    async def run() -> None:
        value = ha_mcp_backend(tmp_path)
        network_called = False

        def forbidden_session():
            nonlocal network_called
            network_called = True
            raise AssertionError("network must not be touched")

        value._session = forbidden_session  # type: ignore[method-assign]
        with pytest.raises(PermissionError, match="not allowed"):
            await value.call_readonly("ha_restart", {"confirm": True})
        assert not network_called

    asyncio.run(run())


def test_unknown_mcp_server_profile_fails_closed_and_clears_context(tmp_path: Path) -> None:
    async def run() -> None:
        value = legacy_backend(tmp_path)
        value._context_cache = {"old": {"stale": True}}
        fake = FakeClient({"some_other_server_tool"})
        value._session = lambda: (FakeHTTPClient(), fake)  # type: ignore[method-assign]
        assert not await value.refresh_context()
        assert value._context_cache == {}
        health = await value.health()
        assert health["connected"] is False
        assert "unsupported MCP server tool profile" in health["error"]

    asyncio.run(run())


def test_secret_path_is_redacted_from_errors_and_audit(tmp_path: Path) -> None:
    value = ha_mcp_backend(tmp_path)
    raw = f"connection failed for {value.url} path /private_testSecretPath"
    safe = value._safe_error(raw)
    assert "private_testSecretPath" not in safe
    assert "<MCP_URL>" in safe or "<MCP_SECRET_PATH>" in safe


def test_disabled_mcp_never_opens_session(tmp_path: Path) -> None:
    async def run() -> None:
        value = legacy_backend(tmp_path, enabled=False)
        value._session = lambda: (_ for _ in ()).throw(AssertionError("network touched"))  # type: ignore[method-assign]
        await value.start()
        assert value.get_relevant_config() == {}
        health = await value.health()
        assert health["enabled"] is False
        assert health["connected"] is False
        assert health["mode"] == "read-only"
        assert health["server_profile"] == "unknown"
        await value.close()

    asyncio.run(run())


def test_mcp_url_rejects_credentials_query_fragment_and_non_http(tmp_path: Path) -> None:
    value = legacy_backend(tmp_path)
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
