from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class LogEvent:
    level: str
    source: str
    exception: str
    message: str
    name: str
    timestamp: float

    @classmethod
    def from_event_data(cls, data: dict[str, Any]) -> "LogEvent":
        raw_message = data.get("message", "")
        if isinstance(raw_message, list):
            message = " ".join(str(x) for x in raw_message)
        else:
            message = str(raw_message)
        return cls(
            level=str(data.get("level", "ERROR")).upper(),
            source=str(data.get("source", "")),
            exception=str(data.get("exception", "") or ""),
            message=message,
            name=str(data.get("name", "")),
            timestamp=float(data.get("timestamp") or datetime.now(tz=timezone.utc).timestamp()),
        )


@dataclass
class Analysis:
    summary: str
    root_cause: str
    confidence: float
    risk: str
    action: str
    affected_files: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    proposed_changes: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
