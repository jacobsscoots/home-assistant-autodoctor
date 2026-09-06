from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.log_filters import NonfatalSuppressionCoalescingFilter


def _record(pattern: str = "kasa/poll", family: str = "kasa") -> logging.LogRecord:
    return logging.LogRecord(
        name="autodoctor.case_engine",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=NonfatalSuppressionCoalescingFilter.PREFIX,
        args=(pattern, family),
        exc_info=None,
    )


def test_suppressed_nonfatal_logs_first_then_periodic_summary() -> None:
    filter_ = NonfatalSuppressionCoalescingFilter(every=3)
    first = _record()
    second = _record()
    third = _record()

    assert filter_.filter(first) is True
    assert filter_.filter(second) is False
    assert filter_.filter(third) is True
    assert "coalesced_occurrences=%s" in str(third.msg)
    assert third.args == ("kasa/poll", "kasa", 3)


def test_unrelated_logs_are_untouched() -> None:
    filter_ = NonfatalSuppressionCoalescingFilter(every=3)
    record = logging.LogRecord(
        name="autodoctor.case_engine",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Starting analysis",
        args=(),
        exc_info=None,
    )
    assert filter_.filter(record) is True
    assert record.msg == "Starting analysis"
