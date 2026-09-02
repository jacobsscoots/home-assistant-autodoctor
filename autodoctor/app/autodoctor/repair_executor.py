from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import AUTODOCTOR_VERSION

_EXECUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS repair_executions (
    execution_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    pattern_key TEXT NOT NULL,
    repair_type TEXT NOT NULL,
    target TEXT NOT NULL,
    started_at REAL NOT NULL,
    baseline_last_seen REAL NOT NULL,
    status TEXT NOT NULL,
    verification_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_repair_executions_plan
ON repair_executions(plan_id, started_at DESC);
"""

_ENTRY_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_ALLOWED_OPERATIONS = {"reload_config_entry", "reload_integration"}
_UNHEALTHY_STATES = {"setup_error", "setup_retry", "not_loaded", "failed", "error"}
_HEALTHY_STATES = {"loaded"}


class RepairExecutor:
    """Explicit-approval executor for a tiny deterministic repair allowlist.

    v0.4.0 supports exactly one Home Assistant mutation: reloading one config entry.
    The operation is non-persistent and is only executed after an authenticated ingress
    approval plus independent validation that the exact entry_id is unambiguous in the
    read-only MCP evidence saved with the plan. There is no generic service-call path.
    """

    def __init__(self, settings: Any, db_path: str, ha: Any, mcp: Any, cases: Any) -> None:
        self.enabled = bool(getattr(settings, "repair_executor_enabled", False))
        self.verification_seconds = max(
            30, min(int(getattr(settings, "repair_verification_seconds", 120)), 3600)
        )
        self.db_path = db_path
        self.ha = ha
        self.mcp = mcp
        self.cases = cases
        self.approval_nonce = secrets.token_urlsafe(32)
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.executescript(_EXECUTION_SCHEMA)
            db.commit()

    @staticmethod
    def _now() -> float:
        return datetime.now(tz=timezone.utc).timestamp()

    async def close(self) -> None:
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_plan_sync, plan_id)

    def _get_plan_sync(self, plan_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM repair_plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            for field, default in (
                ("proposed_changes_json", []),
                ("checks_json", []),
                ("evidence_json", {}),
            ):
                raw = item.pop(field)
                try:
                    item[field.removesuffix("_json")] = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    item[field.removesuffix("_json")] = default
            return item

    @classmethod
    def _entry_ids_from_evidence(cls, value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() == "entry_id" and isinstance(nested, str):
                    if _ENTRY_ID.fullmatch(nested):
                        found.add(nested)
                found.update(cls._entry_ids_from_evidence(nested))
        elif isinstance(value, (list, tuple)):
            for nested in value:
                found.update(cls._entry_ids_from_evidence(nested))
        return found

    @staticmethod
    def _plan_change(plan: dict[str, Any]) -> dict[str, Any]:
        changes = plan.get("proposed_changes")
        if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict):
            raise ValueError("repair plan must contain exactly one structured change")
        return dict(changes[0])

    def validate_plan(self, plan: dict[str, Any]) -> tuple[bool, str, str | None]:
        if not self.enabled:
            return False, "repair executor is disabled", None
        if str(plan.get("status")) != "proposed":
            return False, "repair plan is not awaiting approval", None
        if str(plan.get("repair_type")) != "reload_config_entry":
            return False, "repair type is not in the v0.4 deterministic allowlist", None
        if str(plan.get("risk")) != "low":
            return False, "only low-risk plans are executable", None
        if float(plan.get("confidence") or 0) < 0.90:
            return False, "confidence is below the 0.90 execution threshold", None
        try:
            change = self._plan_change(plan)
        except ValueError as exc:
            return False, str(exc), None
        operation = str(change.get("operation") or change.get("action") or "").strip().lower()
        if operation not in _ALLOWED_OPERATIONS:
            return False, "proposed operation does not match the repair type", None
        target = str(change.get("target") or "").strip()
        if not _ENTRY_ID.fullmatch(target):
            return False, "target is not a valid config-entry identifier", None
        evidence_ids = self._entry_ids_from_evidence(plan.get("evidence") or {})
        if evidence_ids != {target}:
            return False, "target is not the single unambiguous config entry in stored MCP evidence", None
        return True, "approved", target

    async def approve_and_execute(self, plan_id: str) -> dict[str, Any]:
        plan = await self.get_plan(plan_id)
        if plan is None:
            raise LookupError("repair plan not found")
        allowed, reason, target = self.validate_plan(plan)
        if not allowed or target is None:
            raise PermissionError(reason)

        case = await self.cases.get_case(str(plan["pattern_key"]))
        if case is None:
            raise RuntimeError("incident case no longer exists")
        started = self._now()
        execution_id = "exec_" + secrets.token_hex(10)
        await self._start_execution(plan, execution_id, target, started, float(case["last_seen"]))
        try:
            # Fixed service + fixed payload shape. No caller can choose domain/service.
            await self.ha.reload_config_entry(target)
        except Exception as exc:
            await self._fail_execution(
                plan,
                execution_id,
                "Home Assistant rejected the approved config-entry reload.",
            )
            raise RuntimeError("approved config-entry reload failed") from exc

        await self._mark_verifying(plan, execution_id)
        self._schedule_verification(execution_id)
        return {
            "plan_id": plan_id,
            "execution_id": execution_id,
            "status": "verifying",
            "verification_seconds": self.verification_seconds,
        }

    async def reject_plan(self, plan_id: str) -> None:
        plan = await self.get_plan(plan_id)
        if plan is None:
            raise LookupError("repair plan not found")
        if str(plan.get("status")) != "proposed":
            raise ValueError("only proposed plans can be rejected")
        await self._update_plan_status(plan_id, "rejected", error="rejected by user")
        await self.cases.mark_needs_user_action(
            str(plan["pattern_key"]),
            "Repair proposal was rejected; case remains open for manual review.",
        )

    async def _start_execution(
        self,
        plan: dict[str, Any],
        execution_id: str,
        target: str,
        started: float,
        baseline_last_seen: float,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._start_execution_sync,
                plan,
                execution_id,
                target,
                started,
                baseline_last_seen,
            )

    def _start_execution_sync(
        self,
        plan: dict[str, Any],
        execution_id: str,
        target: str,
        started: float,
        baseline_last_seen: float,
    ) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """UPDATE repair_plans SET status='approved', approved_at=?, updated_at=?, error=''
                WHERE plan_id=? AND status='proposed'""",
                (started, started, plan["plan_id"]),
            )
            if db.total_changes != 1:
                raise RuntimeError("repair plan approval race detected")
            db.execute(
                """INSERT INTO repair_executions
                (execution_id, plan_id, pattern_key, repair_type, target, started_at,
                 baseline_last_seen, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'executing')""",
                (
                    execution_id,
                    plan["plan_id"],
                    plan["pattern_key"],
                    plan["repair_type"],
                    target,
                    started,
                    baseline_last_seen,
                ),
            )
            db.execute(
                "UPDATE incident_cases SET status='verifying', updated_at=? WHERE pattern_key=?",
                (started, plan["pattern_key"]),
            )
            db.commit()

    async def _mark_verifying(self, plan: dict[str, Any], execution_id: str) -> None:
        now = self._now()
        async with self._lock:
            await asyncio.to_thread(self._mark_verifying_sync, plan["plan_id"], execution_id, now)
        await self.cases.publish_case(str(plan["pattern_key"]), force=True)

    def _mark_verifying_sync(self, plan_id: str, execution_id: str, now: float) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE repair_plans SET status='verifying', executed_at=?, updated_at=? WHERE plan_id=?",
                (now, now, plan_id),
            )
            db.execute(
                "UPDATE repair_executions SET status='verifying' WHERE execution_id=?",
                (execution_id,),
            )
            db.commit()

    def _schedule_verification(self, execution_id: str) -> None:
        task = asyncio.create_task(
            self._verify_after_window(execution_id),
            name=f"autodoctor-verify-{execution_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def resume_pending_verifications(self) -> int:
        async with self._lock:
            ids = await asyncio.to_thread(self._pending_verification_ids_sync)
        for execution_id in ids:
            self._schedule_verification(execution_id)
        return len(ids)

    def _pending_verification_ids_sync(self) -> list[str]:
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT execution_id FROM repair_executions WHERE status='verifying'"
            ).fetchall()
            return [str(row[0]) for row in rows]

    async def _verify_after_window(self, execution_id: str) -> None:
        execution = await self._get_execution(execution_id)
        if not execution:
            return
        elapsed = max(0.0, self._now() - float(execution["started_at"]))
        remaining = max(0.0, float(self.verification_seconds) - elapsed)
        if remaining:
            await asyncio.sleep(remaining)
        await self._verify_execution(execution_id)

    async def _get_execution(self, execution_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_execution_sync, execution_id)

    def _get_execution_sync(self, execution_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM repair_executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            return dict(row) if row else None

    @classmethod
    def _states_from_result(cls, value: Any) -> set[str]:
        states: set[str] = set()
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in {"state", "status"} and isinstance(nested, str):
                    states.add(nested.strip().lower())
                states.update(cls._states_from_result(nested))
        elif isinstance(value, (list, tuple)):
            for nested in value:
                states.update(cls._states_from_result(nested))
        return states

    async def _integration_health(self, target: str) -> tuple[bool | None, dict[str, Any]]:
        try:
            result = await self.mcp.call_readonly(
                "ha_get_integration",
                {"entry_id": target},
                purpose="post-repair config-entry verification",
            )
        except Exception:
            return None, {"mcp_read": "unavailable"}
        states = self._states_from_result(result)
        if states & _UNHEALTHY_STATES:
            return False, {"mcp_read": "ok", "observed_states": sorted(states)}
        if states & _HEALTHY_STATES:
            return True, {"mcp_read": "ok", "observed_states": sorted(states)}
        return None, {"mcp_read": "ok", "observed_states": sorted(states)}

    async def _verify_execution(self, execution_id: str) -> None:
        execution = await self._get_execution(execution_id)
        if not execution or str(execution.get("status")) != "verifying":
            return
        plan = await self.get_plan(str(execution["plan_id"]))
        if plan is None:
            return
        case = await self.cases.get_case(str(execution["pattern_key"]))
        if case is None:
            return
        recurred = float(case["last_seen"]) > float(execution["started_at"])
        healthy, evidence = await self._integration_health(str(execution["target"]))
        evidence["incident_recurred_after_execution"] = recurred
        evidence["verification_window_seconds"] = self.verification_seconds

        if healthy is True and not recurred:
            await self._complete_success(plan, execution, evidence)
            return
        if healthy is False or recurred:
            reason = (
                "incident recurred during verification"
                if recurred
                else "integration did not return to a healthy loaded state"
            )
            await self._fail_execution(plan, execution_id, reason, evidence=evidence)
            return
        await self._fail_execution(
            plan,
            execution_id,
            "verification was inconclusive; no resolved claim was made",
            evidence=evidence,
            status="verification_inconclusive",
        )

    async def _complete_success(
        self,
        plan: dict[str, Any],
        execution: dict[str, Any],
        evidence: dict[str, Any],
    ) -> None:
        now = self._now()
        async with self._lock:
            await asyncio.to_thread(
                self._complete_success_sync,
                plan,
                execution,
                evidence,
                now,
            )
        await self.cases.mark_resolved(str(plan["pattern_key"]), verification="verified reload")

    def _complete_success_sync(
        self,
        plan: dict[str, Any],
        execution: dict[str, Any],
        evidence: dict[str, Any],
        now: float,
    ) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            db.execute(
                """UPDATE repair_plans SET status='succeeded', verified_at=?, updated_at=?, error=''
                WHERE plan_id=?""",
                (now, now, plan["plan_id"]),
            )
            db.execute(
                """UPDATE repair_executions SET status='succeeded', verification_json=?, error=''
                WHERE execution_id=?""",
                (json.dumps(evidence, separators=(",", ":")), execution["execution_id"]),
            )
            case = db.execute(
                "SELECT * FROM incident_cases WHERE pattern_key=?", (plan["pattern_key"],)
            ).fetchone()
            if case:
                self._persist_verified_fix(db, plan, dict(case), evidence, now)
            db.commit()

    @staticmethod
    def _persist_verified_fix(
        db: sqlite3.Connection,
        plan: dict[str, Any],
        case: dict[str, Any],
        evidence: dict[str, Any],
        now: float,
    ) -> None:
        memory_key = f"repair:{plan['plan_id']}"
        verification = (
            "User-approved deterministic config-entry reload; read-only MCP showed the "
            "integration loaded and the incident did not recur during the verification window."
        )
        metadata = json.dumps(
            {
                "repair_type": "reload_config_entry",
                "plan_id": plan["plan_id"],
                "verification": evidence,
            },
            separators=(",", ":"),
        )
        db.execute(
            """INSERT INTO knowledge
            (memory_key, fingerprint, pattern_key, pattern_label, family, trust_class,
             trust_score, source, root_cause, resolution, verification, outcome,
             ha_version, autodoctor_version, created_at, verified_at, last_confirmed_at,
             expires_at, superseded_by, recurrence_count, baseline_occurrences, metadata_json)
            VALUES (?, ?, ?, ?, ?, 'verified_fix', 1.0, 'autodoctor-approved-repair',
                    ?, ?, ?, 'verified', 'unknown', ?, ?, ?, ?, ?, NULL, 0, ?, ?)
            ON CONFLICT(memory_key) DO UPDATE SET
                trust_class='verified_fix', trust_score=1.0, verification=excluded.verification,
                outcome='verified', verified_at=excluded.verified_at,
                last_confirmed_at=excluded.last_confirmed_at, metadata_json=excluded.metadata_json""",
            (
                memory_key,
                str(plan.get("fingerprint") or ""),
                str(plan["pattern_key"]),
                str(case.get("pattern_label") or ""),
                str(case.get("family") or "unknown"),
                str(plan.get("root_cause") or "")[:4000],
                "Approved deterministic config-entry reload completed without a durable configuration edit.",
                verification,
                AUTODOCTOR_VERSION,
                now,
                now,
                now,
                now + 365 * 86400,
                int(case.get("occurrences") or 0),
                metadata,
            ),
        )
        fts_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
        ).fetchone()
        if fts_exists:
            db.execute("DELETE FROM knowledge_fts WHERE memory_key=?", (memory_key,))
            db.execute(
                """INSERT INTO knowledge_fts
                (memory_key, family, pattern_label, root_cause, resolution, verification, outcome)
                VALUES (?, ?, ?, ?, ?, ?, 'verified')""",
                (
                    memory_key,
                    str(case.get("family") or "unknown"),
                    str(case.get("pattern_label") or ""),
                    str(plan.get("root_cause") or "")[:4000],
                    "Approved deterministic config-entry reload completed.",
                    verification,
                ),
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
        async with self._lock:
            await asyncio.to_thread(
                self._fail_execution_sync,
                plan["plan_id"],
                execution_id,
                status,
                error,
                evidence or {},
                self._now(),
            )
        await self.cases.mark_needs_user_action(
            str(plan["pattern_key"]),
            f"Approved repair did not verify successfully: {error}",
        )

    def _fail_execution_sync(
        self,
        plan_id: str,
        execution_id: str,
        status: str,
        error: str,
        evidence: dict[str, Any],
        now: float,
    ) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE repair_plans SET status=?, updated_at=?, error=? WHERE plan_id=?",
                (status, now, str(error)[:1000], plan_id),
            )
            db.execute(
                """UPDATE repair_executions SET status=?, verification_json=?, error=?
                WHERE execution_id=?""",
                (
                    status,
                    json.dumps(evidence, separators=(",", ":")),
                    str(error)[:1000],
                    execution_id,
                ),
            )
            db.commit()

    async def _update_plan_status(self, plan_id: str, status: str, *, error: str = "") -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_plan_status_sync, plan_id, status, error)

    def _update_plan_status_sync(self, plan_id: str, status: str, error: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE repair_plans SET status=?, updated_at=?, error=? WHERE plan_id=?",
                (status, self._now(), str(error)[:1000], plan_id),
            )
            db.commit()

    async def health(self) -> dict[str, Any]:
        async with self._lock:
            pending = await asyncio.to_thread(self._pending_verification_ids_sync)
        return {
            "enabled": self.enabled,
            "approval_required": True,
            "supported_repairs": ["reload_config_entry"],
            "minimum_confidence": 0.90,
            "verification_seconds": self.verification_seconds,
            "pending_verifications": len(pending),
            "auto_apply_enabled": False,
        }
