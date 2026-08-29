from __future__ import annotations

from typing import Any

from .config import Settings


class MCPBackend:
    """Optional client for ganhammar/hass-mcp-server.

    v0.1 only verifies connectivity and discovers repair capabilities. It deliberately
    does not fetch or mutate automation/script configuration because Home Assistant
    entity IDs are not the same identifier shape required by the upstream config tools.
    v0.2 will add explicit, tested identifier resolution before configuration access.
    """

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.mcp_enabled
        self.url = settings.mcp_url
        self.token = settings.mcp_token

    async def _session(self):
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        http_client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx2.Timeout(30.0, read=120.0),
            follow_redirects=True,
        )
        transport = streamable_http_client(self.url, http_client=http_client)
        return http_client, Client(transport)

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "connected": False}
        if not self.url or not self.token:
            return {"enabled": True, "connected": False, "error": "mcp_url/mcp_token missing"}
        try:
            http_client, client = await self._session()
            async with http_client:
                async with client:
                    tools = await client.list_tools()
                    names = [tool.name for tool in tools.tools]
                    return {
                        "enabled": True,
                        "connected": True,
                        "tools": names,
                        "safe_repair_tools_present": all(
                            name in names
                            for name in (
                                "backup_config_files",
                                "restore_config_backup",
                            )
                        ),
                    }
        except Exception as exc:
            return {"enabled": True, "connected": False, "error": str(exc)}

    async def get_relevant_config(self, entity_ids: list[str]) -> dict[str, Any]:
        # Deliberately empty in v0.1. Do not guess upstream automation_id/script key
        # from a Home Assistant entity_id. Incorrect identifier resolution is worse
        # than giving the AI less context.
        return {}
