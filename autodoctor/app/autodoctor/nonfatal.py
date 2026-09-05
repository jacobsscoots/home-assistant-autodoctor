from __future__ import annotations

from .models import LogEvent

_TPLINK_COORDINATOR_SOURCE = "components/tplink/coordinator.py"
_KASA_FAMILY = "kasa"


def nonfatal_observation_reason(event: LogEvent, family: str) -> str | None:
    """Classify raw python-kasa sub-call logs that are evidence, not a repair target.

    Live Home Assistant evidence established that raw ``kasa.*`` log records emitted
    from TP-Link's configured-entry update coordinator can occur repeatedly while the
    coordinator update itself succeeds and entities remain healthy. A genuine TP-Link
    coordinator failure is emitted separately under Home Assistant's own
    ``homeassistant.components.tplink.coordinator`` logger and therefore does not match
    this rule.

    Keep this deliberately narrow and structural. We do not inspect device names,
    addresses, titles, config-entry identifiers, or fuzzy message text. Unknown Kasa
    sources remain actionable and continue through the normal diagnosis path.
    """

    if str(family or "").strip().lower() != _KASA_FAMILY:
        return None

    logger_name = str(event.name or "").strip().lower()
    if logger_name != _KASA_FAMILY and not logger_name.startswith(f"{_KASA_FAMILY}."):
        return None

    source = str(event.source or "").replace("\\", "/").lower()
    if _TPLINK_COORDINATOR_SOURCE not in source:
        return None

    return (
        "Raw python-kasa sub-call log emitted during Home Assistant TP-Link configured-entry polling; "
        "retained as correlation evidence only. A coordinator-level Home Assistant failure remains actionable."
    )
