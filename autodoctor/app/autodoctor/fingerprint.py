from __future__ import annotations

import hashlib
import re

from .models import LogEvent
from .redact import redact

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,36}\b")
_NUMBER = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?(?![A-Za-z_])")
_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_WS = re.compile(r"\s+")


def normalize_for_fingerprint(text: str) -> str:
    text = redact(text)
    text = _UUID.sub("<UUID>", text)
    text = _HEX_ADDR.sub("<HEX>", text)
    text = _NUMBER.sub("<N>", text)
    return _WS.sub(" ", text).strip().lower()


def fingerprint(event: LogEvent) -> str:
    exception_head = event.exception.splitlines()[0] if event.exception else ""
    payload = "|".join(
        (
            event.level,
            event.name,
            event.source,
            normalize_for_fingerprint(event.message),
            normalize_for_fingerprint(exception_head),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
