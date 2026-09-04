from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .match import entry_data_matches_host, normalize_private_ipv4

DOMAIN = "autodoctor_private_resolver"
_COMMAND = "autodoctor_private_resolver/match_tplink_host"
_TPLINK_DOMAIN = "tplink"


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): _COMMAND,
        vol.Required("host"): cv.string,
    }
)
@callback
def websocket_match_tplink_host(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return TP-Link config entries whose private stored host exactly matches.

    This command is intentionally narrow and read-only. It never returns the queried
    host, ConfigEntry.data, titles, credentials, MAC addresses, or other configuration.
    Only exact RFC1918 IPv4 equality against ConfigEntry.data['host'] is accepted.
    """

    host = normalize_private_ipv4(msg.get("host"))
    if host is None:
        connection.send_error(
            msg["id"],
            "invalid_format",
            "host must be a literal RFC1918 IPv4 address",
        )
        return

    matches: list[dict[str, str]] = []
    for entry in hass.config_entries.async_entries(_TPLINK_DOMAIN):
        if not entry_data_matches_host(entry.data, host):
            continue
        state = getattr(entry.state, "value", str(entry.state))
        matches.append({"entry_id": entry.entry_id, "state": str(state)})

    connection.send_result(
        msg["id"],
        {
            "domain": _TPLINK_DOMAIN,
            "count": len(matches),
            "matches": matches,
        },
    )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the fixed read-only AutoDoctor resolver command."""

    websocket_api.async_register_command(hass, websocket_match_tplink_host)
    return True
