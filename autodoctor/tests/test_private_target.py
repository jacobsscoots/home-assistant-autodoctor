from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.models import Analysis, LogEvent
from autodoctor.private_target import (
    AUTO_RESOLVE_TARGET,
    bind_private_reload_target,
    integration_domain_for_event,
)


def _analysis(*, risk: str = "low", confidence: float = 0.95, target: str = AUTO_RESOLVE_TARGET) -> Analysis:
    return Analysis(
        summary="reload may recover the integration",
        root_cause="transient integration failure",
        confidence=confidence,
        risk=risk,
        action="propose_fix",
        proposed_changes=[
            {
                "operation": "reload_config_entry",
                "target": target,
                "reason": "retry setup without changing configuration",
            }
        ],
    )


def _evidence(*entry_ids: str) -> dict:
    return {
        "private_target_resolution": {
            "integration_domain": "tplink",
            "candidates": [{"entry_id": entry_id} for entry_id in entry_ids],
        }
    }


def test_integration_domain_resolution_is_explicit_and_conservative() -> None:
    kasa = LogEvent("ERROR", "kasa.transports.klaptransport", "", "failed", "kasa", 1.0)
    assert integration_domain_for_event(kasa, "kasa") == "tplink"

    blink = LogEvent("ERROR", "blinkpy.auth", "", "failed", "blinkpy", 1.0)
    assert integration_domain_for_event(blink, "blinkpy") == "blink"

    onedrive = LogEvent(
        "ERROR",
        "homeassistant.components.onedrive.coordinator",
        "",
        "failed",
        "homeassistant.components.onedrive",
        1.0,
    )
    assert integration_domain_for_event(onedrive, "homeassistant.components.onedrive") == "onedrive"

    unknown = LogEvent("ERROR", "aiohttp.client", "", "failed", "aiohttp", 1.0)
    assert integration_domain_for_event(unknown, "aiohttp") is None


def test_private_binding_injects_exactly_one_target_after_ai() -> None:
    analysis = _analysis()
    result = bind_private_reload_target(analysis, _evidence("entry1234"))
    assert result == "bound"
    assert analysis.action == "propose_fix"
    assert analysis.proposed_changes[0]["target"] == "entry1234"


def test_private_binding_refuses_zero_or_multiple_candidates() -> None:
    zero = _analysis()
    assert "found 0" in bind_private_reload_target(zero, _evidence())
    assert zero.action == "observe"
    assert zero.proposed_changes == []

    multiple = _analysis()
    assert "found 2" in bind_private_reload_target(
        multiple,
        _evidence("entry1234", "entry5678"),
    )
    assert multiple.action == "observe"
    assert multiple.proposed_changes == []


def test_private_binding_refuses_model_supplied_identifier() -> None:
    analysis = _analysis(target="hallucinated123")
    result = bind_private_reload_target(analysis, _evidence("entry1234"))
    assert "AI attempted to supply a target" in result
    assert analysis.action == "observe"
    assert analysis.proposed_changes == []


def test_private_binding_preserves_existing_risk_and_confidence_gates() -> None:
    medium = _analysis(risk="medium")
    assert "only low-risk" in bind_private_reload_target(medium, _evidence("entry1234"))
    assert medium.action == "observe"

    low_confidence = _analysis(confidence=0.89)
    assert "below the 0.90" in bind_private_reload_target(
        low_confidence,
        _evidence("entry1234"),
    )
    assert low_confidence.action == "observe"


def test_private_binding_logs_outcome_without_private_identifier(caplog) -> None:
    caplog.set_level(logging.INFO, logger="autodoctor.private_target")
    analysis = _analysis()
    assert bind_private_reload_target(analysis, _evidence("entry1234")) == "bound"
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Private target binding result=bound candidates=1" in text
    assert "entry1234" not in text

    caplog.clear()
    rejected = _analysis(target="do-not-log-this-target")
    bind_private_reload_target(rejected, _evidence("entry1234"))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "do-not-log-this-target" not in text
    assert "entry1234" not in text
