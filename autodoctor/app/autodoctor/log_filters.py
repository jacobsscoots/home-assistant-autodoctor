from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any


class NonfatalSuppressionCoalescingFilter(logging.Filter):
    """Keep evidence accounting intact while reducing repeated suppression log noise."""

    PREFIX = "Suppressed non-fatal case analysis pattern=%s family=%s; evidence retained"
    LOGGER = "autodoctor.case_engine"

    def __init__(self, every: int = 500) -> None:
        super().__init__()
        self.every = max(2, int(every))
        self._counts: dict[tuple[str, str], int] = defaultdict(int)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != self.LOGGER or record.msg != self.PREFIX:
            return True
        args: Any = record.args
        if not isinstance(args, tuple) or len(args) < 2:
            return True
        pattern, family = str(args[0]), str(args[1])
        key = (pattern, family)
        self._counts[key] += 1
        count = self._counts[key]
        if count == 1:
            return True
        if count % self.every != 0:
            return False
        record.msg = (
            "Suppressed non-fatal case analysis pattern=%s family=%s; "
            "evidence retained; coalesced_occurrences=%s"
        )
        record.args = (pattern, family, count)
        return True


class KasaIncidentCoalescingFilter(logging.Filter):
    """Coalesce noisy Kasa incident INFO lines without dropping persisted evidence."""

    PREFIX = "Incident %s pattern=%s occurrence=%s %s: %s"
    LOGGER = "autodoctor.engine"

    def __init__(self, every: int = 500) -> None:
        super().__init__()
        self.every = max(2, int(every))
        self._counts: dict[str, int] = defaultdict(int)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != self.LOGGER or record.msg != self.PREFIX:
            return True
        args: Any = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        pattern = str(args[1])
        if not pattern.startswith("kasa/"):
            return True
        self._counts[pattern] += 1
        count = self._counts[pattern]
        if count == 1:
            return True
        if count % self.every != 0:
            return False
        record.msg = (
            "Incident %s pattern=%s occurrence=%s %s: %s; "
            "coalesced_kasa_events=%s"
        )
        record.args = (*args[:5], count)
        return True


def _install_on_handler(handler: logging.Handler, *, every: int) -> None:
    if not any(isinstance(item, NonfatalSuppressionCoalescingFilter) for item in handler.filters):
        handler.addFilter(NonfatalSuppressionCoalescingFilter(every=every))
    if not any(isinstance(item, KasaIncidentCoalescingFilter) for item in handler.filters):
        handler.addFilter(KasaIncidentCoalescingFilter(every=every))


def install_nonfatal_log_coalescing(*, every: int = 500) -> None:
    """Install coalescing at the root-handler boundary used by production logging.

    `logging.basicConfig` creates the root handler before this function is called in
    main.py. Handler-level filters therefore see the exact records that will be emitted,
    regardless of logger propagation details. The filters themselves constrain matching
    to the expected AutoDoctor logger names and exact message templates.
    """

    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers:
            _install_on_handler(handler, every=every)
        return

    # Defensive fallback for tests or alternate embeddings that have not configured a
    # root handler yet. Production main.py does not use this branch.
    case_logger = logging.getLogger("autodoctor.case_engine")
    engine_logger = logging.getLogger("autodoctor.engine")
    if not any(isinstance(item, NonfatalSuppressionCoalescingFilter) for item in case_logger.filters):
        case_logger.addFilter(NonfatalSuppressionCoalescingFilter(every=every))
    if not any(isinstance(item, KasaIncidentCoalescingFilter) for item in engine_logger.filters):
        engine_logger.addFilter(KasaIncidentCoalescingFilter(every=every))
