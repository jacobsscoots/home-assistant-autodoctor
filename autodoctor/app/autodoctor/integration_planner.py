"""Owner-enrolled reload recipe, independent of AI and MCP availability."""
from __future__ import annotations

import time
from typing import Any

from .models import Analysis
from .repair_backup import RepairBlocked

RECIPE_ID = "enrolled_integration_reload_v1"
ORIGIN = "compiled_integration_reload"
# A reviewed small scope. Critical controllers/security/presence/storage are excluded.
RELOAD_DOMAINS = frozenset({"tplink", "hue", "lifx", "wled"})
RELOAD_STATES = frozenset({"setup_retry", "not_loaded"})


def validate_enrollment(settings: Any, target: str, entry: dict[str, Any]) -> None:
    if not settings.integration_reload_repair_enabled or target not in settings.integration_reload_targets:
        raise RepairBlocked("integration_reload_target_not_enrolled")
    if (entry.get("entry_id") != target or entry.get("disabled_by") is not None
            or entry.get("domain") not in RELOAD_DOMAINS or entry.get("state") not in RELOAD_STATES):
        raise RepairBlocked("integration_reload_live_target_not_eligible")


class IntegrationReloadPlanner:
    def __init__(self, settings: Any, cases: Any, ha: Any) -> None:
        self.settings, self.cases, self.ha = settings, cases, ha
        self.plans_created = 0
        self.last_result = "no_matching_observation"

    async def consider(self, target: str, entry: dict, pattern: str, fp: str, confirmations: int) -> bool:
        if confirmations < 2:
            return False
        case = await self.cases.get_case(pattern)
        if not case or case.get("status") in {"repair_available", "verifying", "needs_user_action"}:
            return False
        try:
            validate_enrollment(self.settings, target, entry)
            current = await self.ha.get_config_entry_status(target)
            validate_enrollment(self.settings, target, current)
            if current.get("domain") != entry.get("domain"):
                raise RepairBlocked("integration_reload_domain_changed")
        except RepairBlocked as exc:
            self.last_result = str(exc)
            return False
        except Exception:
            self.last_result = "integration_reload_live_read_unavailable"
            return False
        analysis = Analysis(
            summary="Enrolled integration remains in a recoverable setup state; propose one reload.",
            root_cause="Repeated native status observations show setup_retry or not_loaded; underlying cause is unproven.",
            confidence=1.0, risk="low", action="propose_fix",
            checks=["exact owner enrollment", "fresh native status", "confirmed encrypted backup", "post-reload verification"],
            proposed_changes=[{"operation": "reload_config_entry", "target": target, "recipe_id": RECIPE_ID}],
        )
        plan = await self.cases.apply_analysis(
            pattern_key=pattern, fingerprint=fp, analysis=analysis,
            evidence={"origin": ORIGIN, "recipe_id": RECIPE_ID, "entry_id": target,
                      "domain": current["domain"], "observed_state": current["state"], "observed_at": time.time()},
        )
        self.plans_created += int(plan is not None)
        self.last_result = "compiled_plan_created" if plan else "plan_not_created"
        return plan is not None

    def health(self) -> dict[str, Any]:
        return {"recipe_id": RECIPE_ID, "enabled": self.settings.integration_reload_repair_enabled,
                "enrolled_targets": len(self.settings.integration_reload_targets),
                "plans_created": self.plans_created, "last_result": self.last_result}
