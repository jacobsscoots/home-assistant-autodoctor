from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .database import database_connection
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

    async def initialize(self) -> None:
        await super().initialize()
        async with self._lock:
            await asyncio.to_thread(self._ensure_execution_mode_column_sync)

    def _ensure_execution_mode_column_sync(self) -> None:
        with database_connection(self.db_path) as db:
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(repair_executions)").fetchall()
            }
            if "execution_mode" not in columns:
                db.execute(
                    "ALTER TABLE repair_executions ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'manual'"
                )
                db.commit()

    async def auto_execute(self, plan_id: str) -> dict[str, Any]:
        """Execute through the normal validator, then persist automatic provenance."""

        if not self.auto_apply_enabled:
            raise PermissionError("automatic repair is disabled")
        result = await super().approve_and_execute(plan_id)
        execution_id = str(result.get("execution_id") or "")
        if execution_id:
            async with self._lock:
                await asyncio.to_thread(
                    self._mark_execution_mode_sync,
                    execution_id,
                    "automatic",
                )
        return result

    def _mark_execution_mode_sync(self, execution_id: str, mode: str) -> None:
        with database_connection(self.db_path) as db:
            db.execute(
                "UPDATE repair_executions SET execution_mode=? WHERE execution_id=?",
                (str(mode)[:32], execution_id),
            )
            db.commit()

    @staticmethod
    def _execution_is_automatic(execution: dict[str, Any] | None) -> bool:
        return bool(execution and str(execution.get("execution_mode") or "") == "automatic")

    def _persist_verified_fix(
        self,
        db: sqlite3.Connection,
        plan: dict[str, Any],
        case: dict[str, Any],
        evidence: dict[str, Any],
        now: float,
    ) -> None:
        # Keep the base persistence format, then correct provenance when the repair was
        # initiated automatically. The execution row is durable, so this survives a
        # restart during the verification window.
        super()._persist_verified_fix(db, plan, case, evidence, now)
        execution = db.execute(
            "SELECT execution_mode FROM repair_executions WHERE plan_id=? ORDER BY started_at DESC LIMIT 1",
            (plan["plan_id"],),
        ).fetchone()
        if not execution or str(execution[0] or "") != "automatic":
            return

        memory_key = f"repair:{plan['plan_id']}"
        verification = (
            "Automatically applied deterministic config-entry reload; read-only MCP showed the "
            "integration loaded and the incident did not recur during the verification window."
        )
        resolution = (
            "Automatic deterministic config-entry reload completed without a durable configuration edit."
        )
        db.execute(
            """UPDATE knowledge SET source='autodoctor-auto-repair', resolution=?, verification=?
            WHERE memory_key=?""",
            (resolution, verification, memory_key),
        )
        fts_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
        ).fetchone()
        if fts_exists:
            db.execute(
                "UPDATE knowledge_fts SET resolution=?, verification=? WHERE memory_key=?",
                (resolution, verification, memory_key),
            )

    async def _fail_execution(
        self,
        plan: dict[str, Any],
        execution_id: str,
        error: str,
        *,
        evidence: dict[str, Any] | None = None,
        status: str = "failed",
    ) -> None:
        execution = await self._get_execution(execution_id)
        automatic = self._execution_is_automatic(execution)
        await super()._fail_execution(
            plan,
            execution_id,
            error,
            evidence=evidence,
            status=status,
        )
        if automatic:
            await self.cases.mark_needs_user_action(
                str(plan["pattern_key"]),
                f"Automatic repair did not verify successfully: {error}",
            )

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

    def _is_automatic_candidate(self, plan: dict[str, Any], plan_id: str) -> bool:
        """Select new, unattempted proposals before the existing executor validation."""
        if not plan_id or plan_id in self._attempted_plan_ids:
            return False
        if str(plan.get("status") or "") != "proposed":
            return False
        # Never auto-execute a proposal that predates this process. This prevents
        # enabling the setting or restarting after an upgrade from silently applying
        # an older plan that was previously waiting for human review.
        if float(plan.get("created_at") or 0) < self.started_at:
            return False
        return True

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
            if not self._is_automatic_candidate(plan, plan_id):
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
                result = await self.executor.auto_execute(plan_id)
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
