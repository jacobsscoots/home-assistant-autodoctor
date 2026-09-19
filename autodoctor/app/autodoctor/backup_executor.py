"""Production backup-first executor. All mutating stages are durable and single-flight."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
from typing import Any

from .automatic_repair import AutoApplyRepairExecutor
from .database import database_connection
from .diagnostic_recipe import DiagnosticHAClient, RECIPE_ID, RECIPE_TYPE, compile_repair
from .repair_backup import BackupUncertain, MIB, RepairBlocked, SupervisorBackupClient, positive_size
from .repair_journal import RepairJournal, config_digest

_LOG = logging.getLogger(__name__)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RECOVERABLE = {"setup_error", "setup_retry", "not_loaded"}


class BackupFirstRepairExecutor(AutoApplyRepairExecutor):
    def __init__(self, settings, db_path, ha, mcp, cases, *, backup_client=None, diagnostic_client=None) -> None:
        super().__init__(settings, db_path, ha, mcp, cases)
        self.settings = settings
        self.journal = RepairJournal(db_path)
        self.backups = backup_client or SupervisorBackupClient(ha.session)
        self.diagnostics = diagnostic_client or DiagnosticHAClient(ha)
        self.last_block_reason = ""
        self.last_cleanup_result = "not_run"
        self._verification_ids: set[str] = set()

    async def initialize(self) -> None:
        await super().initialize()
        await self.journal.initialize()

    def validate_plan(self, plan: dict[str, Any]) -> tuple[bool, str, str | None]:
        try:
            confidence = float(plan.get("confidence") or 0)
        except (TypeError, ValueError):
            return False, "invalid_repair_confidence", None
        if not math.isfinite(confidence) or not 0.90 <= confidence <= 1:
            return False, "repair_confidence_below_threshold", None
        if plan.get("repair_type") != RECIPE_TYPE:
            return super().validate_plan(plan)
        if not self.enabled or not self.settings.diagnostic_template_repair_enabled:
            return False, "diagnostic_recipe_not_enabled", None
        if plan.get("status") != "proposed" or plan.get("risk") != "low":
            return False, "diagnostic_plan_not_eligible", None
        try:
            change = self._plan_change(plan)
        except ValueError:
            return False, "diagnostic_plan_must_contain_one_change", None
        if (change.get("operation") != RECIPE_TYPE or change.get("recipe_id") != RECIPE_ID
                or change.get("entity_id") not in self.settings.diagnostic_repair_entities
                or (plan.get("evidence") or {}).get("origin") != "compiled_diagnostic_recipe"):
            return False, "diagnostic_recipe_or_target_not_enrolled", None
        for field in ("before_digest", "after_digest"):
            if not isinstance(change.get(field), str) or not _DIGEST.fullmatch(change[field]):
                return False, "diagnostic_plan_missing_preconditions", None
        target = str(change.get("target") or "")
        return bool(target), "eligible" if target else "missing_target", target or None

    async def approve_and_execute(self, plan_id: str) -> dict[str, Any]:
        return await self._execute_backed_up(plan_id, "manual")

    async def auto_execute(self, plan_id: str) -> dict[str, Any]:
        if not self.auto_apply_enabled:
            raise PermissionError("automatic repair is disabled")
        return await self._execute_backed_up(plan_id, "automatic")

    def _check_backup_settings(self) -> None:
        if not isinstance(self.settings.repair_backup_password, str) or len(self.settings.repair_backup_password) < 12:
            raise RepairBlocked("set_repair_backup_password_at_least_12_characters")
        if self.settings.repair_backup_keep not in (1, 2):
            raise RepairBlocked("repair_backup_keep_must_be_one_or_two")
        if not 16 <= self.settings.repair_backup_max_size_mb <= self.settings.repair_backup_max_total_mb <= 8192:
            raise RepairBlocked("invalid_backup_size_limits")
        if self.settings.repair_backup_min_free_mb < 128:
            raise RepairBlocked("backup_free_space_reserve_too_small")

    async def _preconditions(self, plan, target) -> tuple[dict[str, Any], dict[str, Any]]:
        if plan["repair_type"] == RECIPE_TYPE:
            change = self._plan_change(plan)
            config_id, before = await self.diagnostics.resolve(change["entity_id"])
            if config_id != target:
                raise RepairBlocked("diagnostic_identity_changed")
            after = compile_repair(before)
            if (config_digest(before) != change["before_digest"]
                    or config_digest(after) != change["after_digest"]):
                raise RepairBlocked("diagnostic_config_changed_since_proposal")
            return before, after
        entry = await self.ha.get_config_entry_status(target)
        if entry.get("entry_id") != target or entry.get("disabled_by") is not None:
            raise RepairBlocked("reload_target_missing_or_disabled")
        if entry.get("state") not in _RECOVERABLE:
            raise RepairBlocked("reload_target_not_proven_unhealthy")
        return {}, {}

    async def _execute_backed_up(self, plan_id: str, mode: str) -> dict[str, Any]:
        plan = await self.get_plan(plan_id)
        if not plan:
            raise LookupError("repair plan not found")
        allowed, reason, target = self.validate_plan(plan)
        if not allowed or target is None:
            raise PermissionError(reason)
        attempt = None
        try:
            self._check_backup_settings()
            before, after = await self._preconditions(plan, target)
            attempt = await self.journal.claim(plan, target, mode, self._now(), before, after)
            await self._secure_backup(attempt)
            # Slow backups must not make a previously valid target stale.
            await self._preconditions(plan, target)
            await self._perform_mutation(plan, attempt, before, after)
        except asyncio.CancelledError:
            # The durable stage survives; startup NEVER replays an uncertain mutation.
            raise
        except Exception as exc:
            code = str(exc) if isinstance(exc, RepairBlocked) else "repair_dependency_unavailable"
            self.last_block_reason = code
            if attempt is not None:
                await self._abort(plan, attempt, code, uncertain=isinstance(exc, BackupUncertain))
            raise RepairBlocked(code) from exc
        self.last_block_reason = ""
        self._schedule_verification(attempt["execution_id"])
        return {"plan_id": plan_id, "execution_id": attempt["execution_id"],
                "status": "verifying", "verification_seconds": self.verification_seconds,
                "backup_confirmed": True}

    async def _secure_backup(self, attempt: dict[str, Any]) -> None:
        owned = await self.journal.backups()
        max_bytes = self.settings.repair_backup_max_size_mb * MIB
        used = sum(int(row["backup_size"]) for row in owned)
        if used + max_bytes > self.settings.repair_backup_max_total_mb * MIB:
            raise RepairBlocked("autodoctor_backup_budget_full")
        await self.backups.preflight(self.settings.repair_backup_min_free_mb * MIB + max_bytes)
        eid = attempt["execution_id"]
        await self.journal.advance(eid, "backup_requested")
        slug, job_id = await self.backups.create(
            name="AutoDoctor " + eid, password=self.settings.repair_backup_password,
            marker=self.journal.marker(attempt),
        )
        # Persist identifiers BEFORE follow-up verification can fail or be interrupted.
        await self.journal.advance(eid, "backup_requested", backup_slug=slug, backup_job=job_id)
        await self.backups.verify_job(job_id)
        info = await self.backups.inspect(slug)
        await self.journal.advance(eid, "backup_requested", backup_size=positive_size(info.get("size_bytes")))
        size = self.backups.validate_snapshot(
            info, slug=slug,
            marker=self.journal.marker(attempt), max_bytes=max_bytes,
        )
        await self.journal.advance(eid, "backed_up", backup_size=size)

    async def _perform_mutation(self, plan, attempt, before, after) -> None:
        eid = attempt["execution_id"]
        latest = await self.journal.get(eid)
        # Check existence/ownership again immediately before mutating HA.
        self.backups.validate_snapshot(
            await self.backups.inspect(latest["backup_slug"]), slug=latest["backup_slug"],
            marker=self.journal.marker(latest), max_bytes=self.settings.repair_backup_max_size_mb * MIB,
        )
        await asyncio.to_thread(self._mark_mutation_start, plan, eid)
        if plan["repair_type"] == RECIPE_TYPE:
            await self.diagnostics.write_checked(attempt["target"], before, after)
        else:
            await self.ha.reload_config_entry(attempt["target"])
        now = self._now()
        await self.journal.advance(eid, "verifying", verification_started_at=now)
        await super()._mark_verifying(plan, eid)

    def _mark_mutation_start(self, plan: dict[str, Any], eid: str) -> None:
        with database_connection(self.db_path) as db:
            row = db.execute("SELECT occurrences FROM incident_cases WHERE pattern_key=?", (plan["pattern_key"],)).fetchone()
            if not row:
                raise RepairBlocked("case_disappeared_before_mutation")
            db.execute("UPDATE repair_attempts SET stage='mutation_started', baseline_occurrences=? WHERE execution_id=?", (row[0], eid))
            db.execute("UPDATE repair_executions SET started_at=? WHERE execution_id=?", (self._now(), eid))

    async def _abort(self, plan, attempt, code: str, *, uncertain: bool = False, status: str = "failed", rollback: bool = True) -> None:
        current = await self.journal.get(attempt["execution_id"])
        if current is None or current["stage"] == "succeeded":
            return  # No late task/publication error may undo an already verified repair.
        uncertain = uncertain or current["stage"] == "backup_requested"
        mutation_uncertain = current["stage"] in {"mutation_started", "rollback_started"}
        if rollback and plan["repair_type"] == RECIPE_TYPE and current["stage"] in {"mutation_started", "verifying", "checking"}:
            status = await self._rollback(current)
        if uncertain:
            status = "backup_uncertain"
        elif mutation_uncertain and status not in {"rolled_back", "failed"}:
            uncertain = True
            status = "mutation_uncertain"
        elif mutation_uncertain and plan["repair_type"] != RECIPE_TYPE:
            uncertain = True
            status = "mutation_uncertain"
        await self.journal.advance(current["execution_id"], status, protected=1, uncertain=int(uncertain), error_code=code)
        await super()._fail_execution(plan, current["execution_id"], code, status=status)

    async def _rollback(self, attempt: dict[str, Any]) -> str:
        before = json.loads(attempt["preimage_json"])
        after = json.loads(attempt["postimage_json"])
        try:
            current = await self.diagnostics.read(attempt["target"])
            if config_digest(current) == config_digest(before):
                return "failed"  # Save never applied; do not rewrite anything.
            if config_digest(current) != config_digest(after):
                return "conflict"  # User/intervening edit; never overwrite it.
            await self.journal.advance(attempt["execution_id"], "rollback_started")
            await self.diagnostics.write_checked(attempt["target"], after, before)
            return "rolled_back"
        except Exception:
            return "conflict"

    def _schedule_verification(self, execution_id: str) -> None:
        if execution_id in self._verification_ids:
            return
        self._verification_ids.add(execution_id)
        task = asyncio.create_task(self._verify_after_window(execution_id), name="autodoctor-backed-verification")
        self._tasks.add(task)
        def finished(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            self._verification_ids.discard(execution_id)
            if not done.cancelled() and done.exception() is not None:
                _LOG.error("Repair verification could not persist its result; manual review required")
        task.add_done_callback(finished)

    async def resume_pending_verifications(self) -> int:
        interrupted = await self.journal.recover_interrupted()
        if interrupted:
            _LOG.error("Interrupted backup/repair stages protected; no mutations replayed")
        return await super().resume_pending_verifications()

    async def _verify_after_window(self, execution_id: str) -> None:
        attempt = await self.journal.get(execution_id)
        if attempt is None:
            await super()._verify_after_window(execution_id)  # Existing pre-upgrade verification, read-only.
            return
        since = attempt["verification_started_at"]
        if since is None:
            return
        remaining = max(0, float(since) + self.verification_seconds - self._now())
        if remaining:
            await asyncio.sleep(remaining)
        await self._verify_execution(execution_id)

    async def _verify_execution(self, execution_id: str) -> None:
        attempt = await self.journal.get(execution_id)
        if attempt is None:
            await super()._verify_execution(execution_id)
            return
        if attempt["stage"] != "verifying":
            return
        if attempt["verification_started_at"] is None or self._now() < attempt["verification_started_at"] + self.verification_seconds:
            return
        plan = await self.get_plan(attempt["plan_id"])
        if not plan:
            return
        if not await self.journal.claim_verification(execution_id):
            return
        committed = False
        try:
            if await asyncio.to_thread(self._has_recurred, plan, attempt):
                raise RepairBlocked("incident_recurred_after_mutation")
            healthy, evidence = await self._verification_evidence(plan, attempt)
            if healthy:
                committed = await asyncio.to_thread(self._commit_verified_success, plan, attempt, evidence)
                if not committed:
                    raise RepairBlocked("incident_recurred_after_mutation")
            else:
                await self._abort(plan, attempt, "verification_inconclusive_no_success_claim", status="verification_inconclusive", rollback=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = str(exc) if isinstance(exc, RepairBlocked) else "verification_evidence_unavailable"
            await self._abort(plan, attempt, code, status="verification_inconclusive", rollback=isinstance(exc, RepairBlocked))
        if committed:
            try:
                await self.cases.publish_case(plan["pattern_key"], force=True)
                await self._prune_after_success(attempt["target_key"])
            except Exception:
                _LOG.warning("Verified repair retained; notification/retention follow-up needs attention")

    def _has_recurred(self, plan, attempt) -> bool:
        with database_connection(self.db_path, readonly=True) as db:
            return self._recurrence_in_transaction(db, plan, attempt)

    @staticmethod
    def _recurrence_in_transaction(db, plan, attempt) -> bool:
        row = db.execute("SELECT occurrences FROM incident_cases WHERE pattern_key=?", (plan["pattern_key"],)).fetchone()
        if row is None or row[0] != attempt["baseline_occurrences"]:
            return True
        if plan["repair_type"] == RECIPE_TYPE:
            change = plan["proposed_changes"][0]
            logger = "homeassistant.components." + change["entity_id"]
            since = db.execute("SELECT started_at FROM repair_executions WHERE execution_id=?", (attempt["execution_id"],)).fetchone()[0]
            return db.execute("SELECT 1 FROM incidents WHERE name=? AND last_seen>=? LIMIT 1", (logger, since)).fetchone() is not None
        return False

    async def _verification_evidence(self, plan, attempt) -> tuple[bool, dict[str, Any]]:
        if plan["repair_type"] == RECIPE_TYPE:
            after = json.loads(attempt["postimage_json"])
            current = await self.diagnostics.read(attempt["target"])
            if config_digest(current) != config_digest(after):
                raise RepairBlocked("diagnostic_config_changed_during_verification")
            evidence = {"recipe_id": RECIPE_ID, "postimage_matches": True,
                        "natural_run_verified": await self.diagnostics.natural_run_verified(attempt["target"], attempt["verification_started_at"], after)}
            return evidence["natural_run_verified"], evidence
        entry = await self.ha.get_config_entry_status(attempt["target"])
        if entry.get("entry_id") != attempt["target"]:
            raise RepairBlocked("verification_target_mismatch")
        if entry.get("state") in _RECOVERABLE:
            raise RepairBlocked("integration_not_healthy_after_reload")
        return entry.get("state") == "loaded", {"integration_loaded": entry.get("state") == "loaded"}

    def _commit_verified_success(self, plan, attempt, evidence) -> bool:
        eid = attempt["execution_id"]
        now = self._now()
        evidence = {**evidence, "verification_window_seconds": self.verification_seconds, "backup_confirmed": True}
        with database_connection(self.db_path) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT stage FROM repair_attempts WHERE execution_id=?", (eid,)).fetchone()
            if not current or current["stage"] != "checking":
                return False
            case = db.execute("SELECT * FROM incident_cases WHERE pattern_key=?", (plan["pattern_key"],)).fetchone()
            if not case or self._recurrence_in_transaction(db, plan, attempt):
                return False
            db.execute("UPDATE repair_attempts SET stage='succeeded', protected=0 WHERE execution_id=?", (eid,))
            db.execute("UPDATE repair_executions SET status='succeeded', verification_json=?, error='' WHERE execution_id=?", (json.dumps(evidence), eid))
            db.execute("UPDATE repair_plans SET status='succeeded', verified_at=?, updated_at=?, error='' WHERE plan_id=?", (now, now, plan["plan_id"]))
            db.execute("UPDATE incident_cases SET status='resolved', updated_at=? WHERE pattern_key=?", (now, plan["pattern_key"]))
            self._persist_verified_fix(db, plan, dict(case), evidence, now)
        return True

    def _persist_verified_fix(self, db, plan, case, evidence, now) -> None:
        super()._persist_verified_fix(db, plan, case, evidence, now)
        if plan["repair_type"] == "reload_config_entry":
            verification = "Confirmed encrypted pre-repair backup; native read-only HA status showed the exact entry loaded with no case recurrence."
            db.execute("UPDATE knowledge SET verification=?,metadata_json=? WHERE memory_key=?", (verification, json.dumps({"repair_type": "reload_config_entry", "verification": evidence}), "repair:" + plan["plan_id"]))
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_fts'").fetchone()
            if exists:
                db.execute("UPDATE knowledge_fts SET verification=? WHERE memory_key=?", (verification, "repair:" + plan["plan_id"]))
        if plan["repair_type"] == RECIPE_TYPE:
            resolution = "Compiled diagnostic log-message guard applied; no control actions, triggers or conditions were changed."
            verification = "Stored postimage matched and a natural diagnostic run completed; no case recurrence in the verification window."
            db.execute("UPDATE knowledge SET source='autodoctor-compiled-repair',resolution=?,verification=?,metadata_json=? WHERE memory_key=?", (resolution, verification, json.dumps({"repair_type": RECIPE_TYPE, "verification": evidence}), "repair:" + plan["plan_id"]))
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_fts'").fetchone()
            if exists:
                db.execute("UPDATE knowledge_fts SET resolution=?,verification=? WHERE memory_key=?", (resolution, verification, "repair:" + plan["plan_id"]))

    async def _prune_after_success(self, key: str) -> None:
        rows = await self.journal.backups(key)
        try:
            for row in rows[self.settings.repair_backup_keep:]:
                if row["protected"] or row["stage"] != "succeeded":
                    continue
                self.backups.validate_snapshot(
                    await self.backups.inspect(row["backup_slug"]), slug=row["backup_slug"],
                    marker=self.journal.marker(row), max_bytes=self.settings.repair_backup_max_size_mb * MIB,
                )
                await self.backups.delete_local(row["backup_slug"])
                await self.journal.advance(row["execution_id"], "succeeded", deleted=1)
            self.last_cleanup_result = "complete"
        except Exception:
            # Successful repairs remain successful if optional retention cleanup fails.
            self.last_cleanup_result = "cleanup_needs_attention_no_further_deletions"
            _LOG.warning("AutoDoctor backup retention cleanup needs attention; recovery points retained")

    async def health(self) -> dict[str, Any]:
        data = await super().health()
        data["supported_repairs"] = ["reload_config_entry", RECIPE_TYPE]
        data["backup_safety"] = {
            **await self.journal.health(), "required": True,
            "password_configured": isinstance(self.settings.repair_backup_password, str) and len(self.settings.repair_backup_password) >= 12,
            "keep_per_target_and_type": self.settings.repair_backup_keep,
            "max_total_mb": self.settings.repair_backup_max_total_mb,
            "last_block_reason": self.last_block_reason, "last_cleanup_result": self.last_cleanup_result,
            "automatic_full_restore": False,
        }
        return data
