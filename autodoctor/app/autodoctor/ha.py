from __future__ import annotations

import asyncio
from ipaddress import IPv4Address, IPv4Network, ip_address
import json
import logging
import os
import re
from typing import Any, AsyncIterator

import aiohttp

from .models import LogEvent

_LOG = logging.getLogger(__name__)

_HA_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
_HA_WS_TIMEOUT = aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10)
_HA_WS_HANDSHAKE_TIMEOUT_SECONDS = 15
_CONFIG_ENTRY_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_PRIVATE_IPV4_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
_PRIVATE_TPLINK_RESOLVER_COMMAND = "autodoctor_private_resolver/match_tplink_host"


def _normalize_private_ipv4(value: str) -> str | None:
    try:
        parsed = ip_address(str(value).strip())
    except ValueError:
        return None
    if not isinstance(parsed, IPv4Address):
        return None
    if not any(parsed in network for network in _PRIVATE_IPV4_NETWORKS):
        return None
    return str(parsed)


class HomeAssistantClient:
    def __init__(self) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is unavailable; homeassistant_api must be enabled")
        self.token = token
        self.api_base = "http://supervisor/core/api"
        self.ws_url = "ws://supervisor/core/websocket"
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=_HA_HTTP_TIMEOUT,
        )

    async def close(self) -> None:
        await self.session.close()

    @staticmethod
    async def _receive_handshake_json(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        return await asyncio.wait_for(
            ws.receive_json(),
            timeout=_HA_WS_HANDSHAKE_TIMEOUT_SECONDS,
        )

    async def _subscribe_system_log(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        hello = await self._receive_handshake_json(ws)
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected websocket greeting: {hello.get('type')}")

        await ws.send_json({"type": "auth", "access_token": self.token})
        auth = await self._receive_handshake_json(ws)
        if auth.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant websocket authentication failed")

        await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "system_log_event"})
        ack = await self._receive_handshake_json(ws)
        if not ack.get("success"):
            raise RuntimeError(f"system_log_event subscription failed: {ack}")

    @staticmethod
    def _message_event(msg: aiohttp.WSMessage) -> LogEvent | None:
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None
        data = json.loads(msg.data)
        if data.get("type") != "event":
            return None
        event = data.get("event", {})
        return LogEvent.from_event_data(event.get("data", {}))

    async def _iter_system_log_events(
        self,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> AsyncIterator[LogEvent]:
        async for msg in ws:
            event = self._message_event(msg)
            if event is not None:
                yield event

    async def system_log_events(self) -> AsyncIterator[LogEvent]:
        backoff = 2
        while True:
            try:
                async with self.session.ws_connect(
                    self.ws_url, heartbeat=30, timeout=_HA_WS_TIMEOUT
                ) as ws:
                    await self._subscribe_system_log(ws)
                    _LOG.info("Watching Home Assistant system_log_event stream")
                    backoff = 2
                    async for event in self._iter_system_log_events(ws):
                        yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.warning("HA websocket disconnected: %s; retrying in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        async with self.session.get(f"{self.api_base}/states/{entity_id}") as response:
            if response.status == 404:
                return None
            response.raise_for_status()
            return await response.json()

    async def get_version(self) -> str:
        """Return the live HA Core version without reading configuration files."""
        async with self.session.get(f"{self.api_base}/config") as response:
            response.raise_for_status()
            data = await response.json()
        return str(data.get("version") or "unknown")

    async def match_tplink_config_entries_by_host(self, host: str) -> dict[str, Any]:
        """Privately resolve an RFC1918 TP-Link host through the fixed HA WS command.

        This is deliberately not a generic Home Assistant WebSocket command surface.
        The helper command is fixed, TP-Link-only and read-only. The queried host is
        never logged or returned by this client.
        """

        normalized = _normalize_private_ipv4(host)
        if normalized is None:
            raise ValueError("private TP-Link resolver requires a literal RFC1918 IPv4")

        async with self.session.ws_connect(
            self.ws_url, heartbeat=30, timeout=_HA_WS_TIMEOUT
        ) as ws:
            hello = await self._receive_handshake_json(ws)
            if hello.get("type") != "auth_required":
                raise RuntimeError("unexpected Home Assistant websocket greeting")

            await ws.send_json({"type": "auth", "access_token": self.token})
            auth = await self._receive_handshake_json(ws)
            if auth.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant websocket authentication failed")

            await ws.send_json(
                {
                    "id": 1,
                    "type": _PRIVATE_TPLINK_RESOLVER_COMMAND,
                    "host": normalized,
                }
            )
            response = await self._receive_handshake_json(ws)

        if response.get("type") != "result" or not response.get("success"):
            raise RuntimeError("private TP-Link resolver is unavailable or rejected the request")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("domain") != "tplink":
            raise RuntimeError("private TP-Link resolver returned an invalid result")

        raw_matches = result.get("matches")
        if not isinstance(raw_matches, list):
            raise RuntimeError("private TP-Link resolver returned malformed matches")

        matches: list[dict[str, str]] = []
        for raw in raw_matches:
            if not isinstance(raw, dict):
                raise RuntimeError("private TP-Link resolver returned malformed match data")
            entry_id = str(raw.get("entry_id") or "").strip()
            if not _CONFIG_ENTRY_ID.fullmatch(entry_id):
                raise RuntimeError("private TP-Link resolver returned an invalid config-entry identifier")
            matches.append(
                {
                    "entry_id": entry_id,
                    "state": str(raw.get("state") or "unknown")[:80],
                }
            )

        try:
            declared_count = int(result.get("count"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("private TP-Link resolver returned an invalid count") from exc
        if declared_count != len(matches):
            raise RuntimeError("private TP-Link resolver count does not match its result set")

        return {"domain": "tplink", "count": len(matches), "matches": matches}

    async def notify(self, title: str, message: str, notification_id: str) -> None:
        payload = {"title": title, "message": message, "notification_id": notification_id}
        async with self.session.post(
            f"{self.api_base}/services/persistent_notification/create", json=payload
        ) as response:
            if response.status >= 400:
                _LOG.warning("Could not create persistent notification: HTTP %s", response.status)

    async def dismiss_notification(self, notification_id: str) -> None:
        """Dismiss one known persistent notification by ID.

        Case management only passes IDs AutoDoctor itself generated. This is not a
        general notification cleanup/listing API and does not touch other integrations.
        """
        payload = {"notification_id": notification_id}
        async with self.session.post(
            f"{self.api_base}/services/persistent_notification/dismiss", json=payload
        ) as response:
            if response.status >= 400:
                _LOG.warning("Could not dismiss persistent notification: HTTP %s", response.status)

    async def reload_config_entry(self, entry_id: str) -> None:
        """Reload exactly one validated config entry.

        This fixed endpoint/payload is the entire v0.4.0 repair mutation surface. There
        is intentionally no generic Home Assistant service-call method.
        """
        target = str(entry_id).strip()
        if not _CONFIG_ENTRY_ID.fullmatch(target):
            raise ValueError("invalid config-entry identifier")
        async with self.session.post(
            f"{self.api_base}/services/homeassistant/reload_config_entry",
            json={"entry_id": target},
        ) as response:
            if response.status >= 400:
                _LOG.warning("Approved config-entry reload failed: HTTP %s", response.status)
                response.raise_for_status()
