from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.budget import BudgetReservation
from autodoctor.config import Settings
from autodoctor.engine import AutoDoctorEngine
from autodoctor.models import AIResult, Analysis


@pytest.mark.parametrize("auto_apply,executor_enabled", [(False, False), (True, False), (True, True)])
def test_analysis_does_not_emit_obsolete_executor_warning(caplog, auto_apply: bool, executor_enabled: bool) -> None:
    async def run() -> None:
        store = SimpleNamespace(finalize_ai_usage=AsyncMock(), save_analysis=AsyncMock(), monthly_ai_usage=AsyncMock(return_value={"spent_usd": 0}))
        settings = Settings(auto_apply_low_risk=auto_apply, repair_executor_enabled=executor_enabled, memory_enabled=False)
        engine = AutoDoctorEngine(settings, store, None, None, None)
        result = AIResult(Analysis("Observe", "Transient issue", 0.6, "low", "observe"), 10, 5)
        await engine._handle_success(result, usage_id=1, reservation=BudgetReservation(10, 5, 0), fp="test", family="test", pattern_key="test/pattern", pattern_label="test", row={"occurrences": 1})
        store.save_analysis.assert_awaited_once()

    with caplog.at_level(logging.INFO, logger="autodoctor.engine"):
        asyncio.run(run())
    assert "AI analysis" in caplog.text
    assert "intentionally disabled" not in caplog.text
    assert not [record for record in caplog.records if record.name == "autodoctor.engine" and record.levelno >= logging.WARNING]


def test_genuine_automatic_repair_startup_warning_is_retained() -> None:
    assert "Automatic low-risk repair is enabled; only newly-created plans" in (ROOT / "main.py").read_text()
