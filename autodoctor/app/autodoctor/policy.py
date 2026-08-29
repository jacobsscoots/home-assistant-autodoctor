from __future__ import annotations

import re

from .models import LogEvent

_IMMEDIATE = re.compile(
    r"(?i)(invalid config|config error|setup failed|error setting up|message malformed|"
    r"service .* not found|action .* not found|entity .* not found|integration not found|"
    r"failed to call service|templateerror|undefinederror)"
)
_TRANSIENT = re.compile(
    r"(?i)(timeout|timed out|temporary failure|connection reset|connection refused|"
    r"server disconnected|rate limit|429|502|503|504|dns|network is unreachable)"
)
_IGNORE_NAMES = (
    "autodoctor",
    "homeassistant.components.system_log.external",
)


def should_ignore(event: LogEvent) -> bool:
    lower_name = event.name.lower()
    if any(marker in lower_name for marker in _IGNORE_NAMES):
        return True
    return "autodoctor" in event.message.lower()


def is_immediate(event: LogEvent) -> bool:
    return bool(_IMMEDIATE.search(event.message + "\n" + event.exception))


def looks_transient(event: LogEvent) -> bool:
    return bool(_TRANSIENT.search(event.message + "\n" + event.exception))


def can_auto_apply(analysis_risk: str, action: str, enabled: bool) -> bool:
    # v0.1 intentionally has no autonomous mutation path. Keep the policy function
    # explicit so enabling the UI switch cannot accidentally turn proposals into writes.
    return enabled and analysis_risk == "low" and action == "deterministic_fix" and False
