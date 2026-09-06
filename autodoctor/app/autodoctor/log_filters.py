from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any


class NonfatalSuppressionCoalescingFilter(logging.Filter):
    """Keep evidence accounting intact while reducing repeated suppression log noise.

    The case engine still records every event in SQLite. This filter only affects the
    human-readable INFO line emitted after a known non-fatal event has already been
    retained and suppressed from AI analysis.
    """

    PREFIX = "Suppressed non-fatal case analysis pattern=%s family=%s; evidence retained"

    def __init__(self, every: int = 500) -> None:
        super().__init__()
        self.every = max(2, int(every))
        self._counts: dict[tuple[str, str], int] = defaultdict(int)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != self.PREFIX:
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


def install_nonfatal_log_coalescing(*, every: int = 500) -> None:
    """Install the filter only on AutoDoctor's case-engine logger."""

    logger = logging.getLogger("autodoctor.case_engine")
    if any(isinstance(item, NonfatalSuppressionCoalescingFilter) for item in logger.filters):
        return
    logger.addFilter(NonfatalSuppressionCoalescingFilter(every=every))
