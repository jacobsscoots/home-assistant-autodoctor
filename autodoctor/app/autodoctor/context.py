from __future__ import annotations

import re
from typing import Any

from .ha import HomeAssistantClient
from .models import LogEvent
from .redact import redact

_ENTITY_ID = re.compile(r"\b(?:automation|binary_sensor|button|calendar|camera|climate|counter|cover|device_tracker|event|fan|humidifier|input_boolean|input_button|input_datetime|input_number|input_select|input_text|light|lock|media_player|number|person|remote|scene|script|select|sensor|siren|switch|text|timer|update|vacuum|weather)\.[a-zA-Z0-9_]+\b")


async def collect_context(event: LogEvent, ha: HomeAssistantClient) -> dict[str, Any]:
    combined = f"{event.message}\n{event.exception}"
    entity_ids = list(dict.fromkeys(_ENTITY_ID.findall(combined)))[:20]
    states: dict[str, Any] = {}
    for entity_id in entity_ids:
        try:
            state = await ha.get_state(entity_id)
        except Exception as exc:
            states[entity_id] = {"error": str(exc)}
            continue
        if state is None:
            states[entity_id] = None
        else:
            attrs = state.get("attributes", {})
            states[entity_id] = {
                "state": state.get("state"),
                "last_changed": state.get("last_changed"),
                "friendly_name": attrs.get("friendly_name"),
                "device_class": attrs.get("device_class"),
            }
    return {
        "event": {
            "level": event.level,
            "name": event.name,
            "source": event.source,
            "message": redact(event.message)[:6000],
            "exception": redact(event.exception)[:12000],
        },
        "referenced_entities": states,
    }
