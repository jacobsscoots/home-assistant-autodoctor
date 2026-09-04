from __future__ import annotations

import re
from typing import Any

from .models import Analysis, LogEvent

AUTO_RESOLVE_TARGET = "AUTO_RESOLVE"
_ENTRY_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Explicit logger/library -> Home Assistant integration-domain aliases. Keep this
# deliberately tiny: an unknown library must not be guessed into an integration.
_LIBRARY_DOMAIN_ALIASES = {
    "kasa": "tplink",
    "tplink": "tplink",
    "blinkpy": "blink",
    "blink": "blink",
}


def integration_domain_for_event(event: LogEvent, family: str) -> str | None:
    """Resolve a logger family to an exact HA integration domain without AI choice.

    Native/custom component loggers carry the domain directly. Third-party library
    loggers are accepted only through the explicit alias table above. Unknown
    libraries return None instead of falling back to fuzzy search.
    """

    for raw in (event.name, event.source, family):
        value = str(raw or "").strip().lower()
        parts = [part for part in value.split(".") if part]
        if len(parts) >= 3 and parts[0] == "homeassistant" and parts[1] == "components":
            return parts[2]
        if len(parts) >= 2 and parts[0] == "custom_components":
            return parts[1]

    family_key = str(family or "").strip().lower().split(".", 1)[0]
    return _LIBRARY_DOMAIN_ALIASES.get(family_key)


def entry_ids_from_value(value: Any) -> set[str]:
    """Return syntactically valid config-entry IDs from private evidence."""

    found: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in {"entry_id", "config_entry_id"} and isinstance(nested, str):
                if _ENTRY_ID.fullmatch(nested):
                    found.add(nested)
            found.update(entry_ids_from_value(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found.update(entry_ids_from_value(nested))
    return found


def _downgrade_private_reload(analysis: Analysis, reason: str) -> str:
    """Fail closed without creating an unexecutable repair plan."""

    analysis.action = "observe"
    analysis.proposed_changes = []
    note = f"AutoDoctor private target resolver withheld repair: {reason}."
    if note not in analysis.checks:
        analysis.checks = [*analysis.checks, note][:30]
    return reason


def bind_private_reload_target(analysis: Analysis, evidence: dict[str, Any]) -> str:
    """Bind AUTO_RESOLVE to one private config-entry ID after AI analysis.

    The model is never allowed to choose or echo a real target identifier. Binding is
    permitted only for the existing reload_config_entry repair type and only when the
    existing executor's low-risk/confidence/single-change conditions already hold.
    Any mismatch downgrades the result to observation so no repair plan is created.
    """

    if analysis.action != "propose_fix":
        return "not_requested"
    if len(analysis.proposed_changes) != 1:
        return _downgrade_private_reload(analysis, "repair must contain exactly one proposed change")

    change = analysis.proposed_changes[0]
    operation = str(change.get("operation") or change.get("action") or "").strip().lower()
    if operation not in {"reload_config_entry", "reload_integration"}:
        return "not_reload"

    if analysis.risk != "low":
        return _downgrade_private_reload(analysis, "only low-risk reload proposals may receive a private target")
    if float(analysis.confidence or 0) < 0.90:
        return _downgrade_private_reload(analysis, "confidence is below the 0.90 private-target threshold")

    model_target = str(change.get("target") or "").strip()
    if model_target != AUTO_RESOLVE_TARGET:
        return _downgrade_private_reload(
            analysis,
            "the AI attempted to supply a target instead of requesting AUTO_RESOLVE",
        )

    candidates = entry_ids_from_value(evidence.get("private_target_resolution") or {})
    if len(candidates) != 1:
        return _downgrade_private_reload(
            analysis,
            f"expected exactly one private config-entry candidate, found {len(candidates)}",
        )

    # The identifier enters the repair plan only here, after provider output has been
    # recorded. It was not present in the AI prompt and was not chosen by the model.
    change["target"] = next(iter(candidates))
    return "bound"
