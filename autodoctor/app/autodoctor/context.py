from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .ha import HomeAssistantClient
from .models import LogEvent
from .redact import redact

if TYPE_CHECKING:
    from .store import IncidentStore

_ENTITY_CANDIDATE = re.compile(r"\b[a-z_]+\.\w+\b")
_ENTITY_DOMAINS = frozenset(
    {
        "automation",
        "binary_sensor",
        "button",
        "calendar",
        "camera",
        "climate",
        "counter",
        "cover",
        "device_tracker",
        "event",
        "fan",
        "humidifier",
        "input_boolean",
        "input_button",
        "input_datetime",
        "input_number",
        "input_select",
        "input_text",
        "light",
        "lock",
        "media_player",
        "number",
        "person",
        "remote",
        "scene",
        "script",
        "select",
        "sensor",
        "siren",
        "switch",
        "text",
        "timer",
        "update",
        "vacuum",
        "weather",
    }
)


def _is_supported_entity(candidate: str) -> bool:
    return candidate.partition(".")[0] in _ENTITY_DOMAINS


def _entity_ids(text: str) -> list[str]:
    return [candidate for candidate in _ENTITY_CANDIDATE.findall(text) if _is_supported_entity(candidate)]


def _alias_match(match: re.Match[str], aliases: dict[str, str]) -> str:
    candidate = match.group(0)
    if not _is_supported_entity(candidate):
        return candidate
    return aliases.get(candidate, "<ENTITY>")


def _sanitize_text(text: str, aliases: dict[str, str]) -> str:
    aliased = _ENTITY_CANDIDATE.sub(lambda match: _alias_match(match, aliases), text)
    return redact(aliased)


async def collect_context(
    event: LogEvent,
    ha: HomeAssistantClient,
    store: "IncidentStore",
    family: str,
) -> dict[str, Any]:
    combined = f"{event.name}\n{event.source}\n{event.message}\n{event.exception}"
    entity_ids = list(dict.fromkeys(_entity_ids(combined)))[:20]
    aliases = await store.get_or_create_entity_aliases(entity_ids, event.timestamp)
    states: dict[str, Any] = {}

    for entity_id in entity_ids:
        alias = aliases[entity_id]
        try:
            state = await ha.get_state(entity_id)
        except Exception as exc:
            states[alias] = {"error": _sanitize_text(str(exc), aliases)}
            continue

        if state is None:
            states[alias] = None
        else:
            attrs = state.get("attributes", {})
            states[alias] = {
                "state": state.get("state"),
                "last_changed": state.get("last_changed"),
                "device_class": attrs.get("device_class"),
            }

    await store.observe_topology(entity_ids, aliases, family, event.timestamp)

    return {
        "event": {
            "level": event.level,
            "name": _sanitize_text(event.name, aliases),
            "source": _sanitize_text(event.source, aliases),
            "message": _sanitize_text(event.message, aliases)[:6000],
            "exception": _sanitize_text(event.exception, aliases)[:12000],
        },
        "referenced_entities": states,
    }
