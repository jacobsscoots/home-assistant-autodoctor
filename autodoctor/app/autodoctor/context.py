from __future__ import annotations

import re
from typing import Any

from .ha import HomeAssistantClient
from .models import LogEvent
from .redact import redact

_ENTITY_ID = re.compile(r"\b(?:automation|binary_sensor|button|calendar|camera|climate|counter|cover|device_tracker|event|fan|humidifier|input_boolean|input_button|input_datetime|input_number|input_select|input_text|light|lock|media_player|number|person|remote|scene|script|select|sensor|siren|switch|text|timer|update|vacuum|weather)\.[a-zA-Z0-9_]+\b")


def _entity_aliases(entity_ids: list[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for index, entity_id in enumerate(entity_ids, start=1):
        domain = entity_id.split(".", 1)[0]
        aliases[entity_id] = f"{domain}.entity_{index}"
    return aliases


def _sanitize_text(text: str, aliases: dict[str, str]) -> str:
    aliased = _ENTITY_ID.sub(lambda match: aliases.get(match.group(0), match.group(0)), text)
    return redact(aliased)


async def collect_context(event: LogEvent, ha: HomeAssistantClient) -> dict[str, Any]:
    combined = f"{event.message}\n{event.exception}"
    entity_ids = list(dict.fromkeys(_ENTITY_ID.findall(combined)))[:20]
    aliases = _entity_aliases(entity_ids)
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
