from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.transport_logging import suppress_sensitive_http_transport_logs


def test_sensitive_http_transport_loggers_are_fully_suppressed() -> None:
    names = ("httpx2", "httpcore2", "httpcore2.connection", "httpcore2.http11")
    originals = {name: logging.getLogger(name).level for name in names}
    try:
        for name in names:
            logging.getLogger(name).setLevel(logging.NOTSET)

        suppress_sensitive_http_transport_logs()

        assert logging.getLogger("httpx2").getEffectiveLevel() > logging.CRITICAL
        assert logging.getLogger("httpcore2").getEffectiveLevel() > logging.CRITICAL
        assert logging.getLogger("httpcore2.connection").getEffectiveLevel() > logging.CRITICAL
        assert logging.getLogger("httpcore2.http11").getEffectiveLevel() > logging.CRITICAL

        # A child created after hardening must inherit the silent parent level too.
        future_child = logging.getLogger("httpcore2.http2")
        future_original = future_child.level
        future_child.setLevel(logging.NOTSET)
        try:
            assert future_child.getEffectiveLevel() > logging.CRITICAL
        finally:
            future_child.setLevel(future_original)
    finally:
        for name, level in originals.items():
            logging.getLogger(name).setLevel(level)
