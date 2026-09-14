from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .repair_executor import RepairExecutor

_LOG = logging.getLogger(__name__)


class AutoApplyRepairExecutor(RepairExecutor):
    """Repair executor with an explicit, opt-in automatic execution mode.

    Automatic mode does not relax plan validation. It only removes the human click for
    plans that already pass RepairExecutor.validate_plan(). The setting remains off by
    default and the manual ingress approval path remains available.
    """

    def __init__(self, settings: Any, db_path: str, ha: Any, mcp: Any, cases: Any) -> None:
        super().__init__(settings, db_path, ha, mcp, cases)
        self.auto_apply_enabled = bool(getattr(settings, "auto_apply_low_risk", False))

    async def health(self) -> dict[str, Any]:
        health = await super().health()
        health["auto_apply_enabled"] = bool(self.auto_apply_enabled and self.enabled)
        health["automatic_repairs_require_existing_executor_gates"] = True
        return health


class AutomaticRepairCoordinator:
    """Poll newly-created plans and execute only already-eligible deterministic repairs."""

    def __init__(
        self,
        settings: Any,
        cases: Any,
        executor: AutoApplyRepairExecutor,
        *,
        poll_seconds: float = 5.0,
    ) -> None:
        self.enabled = bool(getattr(settings, "auto_apply_low_risk", False))
        self.cases = cases
        self.executor = executor
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.started_at = datetime.now(tz=timezone.utc).timestamp()
        self._attempted_plan_ids: set[str] = set()

    async def run_once(self) -> int:
        if not self.enabled or not self.executor.enabled:
            return 0

        health = await self.executor.health()
        if int(health.get("pending_verifications") or 0) > 0:
            return 0

        plans = await self.cases.list_repair_plans(100)
        plans.sort(key=lambda plan: float(plan.get("created_at") or 0))
        for plan in plans:
            plan_id = str(plan.get("plan_id") or "")
            if not plan_id or plan_id in self._attempted_plan_ids:
                continue
            if str(plan.get("status") or "") != "proposed":
                continue
            # Never auto-execute a proposal that predates this process. This prevents
            # enabling the setting or restarting after an upgrade from silently applying
            # an older plan that was previously waiting for human review.
            if float(plan.get("created_at") or 0) < self.started_at:
                continue

            allowed, reason, _target = self.executor.validate_plan(plan)
            if not allowed:
                _LOG.info(
                    "Automatic repair withheld plan=%s: %s",
                    plan_id,
                    reason,
                )
                self._attempted_plan_ids.add(plan_id)
                continue

            self._attempted_plan_ids.add(plan_id)
            try:
                result = await self.executor.approve_and_execute(plan_id)
            except (LookupError, PermissionError, ValueError, RuntimeError) as exc:
                _LOG.warning(
                    "Automatic repair failed safely plan=%s: %s",
                    plan_id,
                    exc,
                )
                return 0

            _LOG.warning(
                "Automatic low-risk repair started plan=%s execution=%s; post-repair verification remains mandatory",
                plan_id,
                result.get("execution_id", "unknown"),
            )
            # Only one automatic repair may begin per scan. The pending-verification
            # gate above prevents a second repair while this one is being verified.
            return 1
        return 0

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("Automatic repair coordinator failed safely; monitoring continues")
            await asyncio.sleep(self.poll_seconds)
