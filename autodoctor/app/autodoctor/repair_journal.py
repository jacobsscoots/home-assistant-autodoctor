"""Private durable repair/backup ledger. Never export stored configuration to AI."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
from typing import Any

from .database import database_connection
from .repair_backup import RepairBlocked

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repair_safety_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repair_attempts (
    execution_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL UNIQUE,
    target_key TEXT NOT NULL, target TEXT NOT NULL, repair_type TEXT NOT NULL,
    created_at REAL NOT NULL, verification_started_at REAL,
    stage TEXT NOT NULL, nonce TEXT NOT NULL, owner TEXT NOT NULL,
    backup_slug TEXT NOT NULL DEFAULT '', backup_job TEXT NOT NULL DEFAULT '',
    backup_size INTEGER NOT NULL DEFAULT 0, protected INTEGER NOT NULL DEFAULT 1,
    deleted INTEGER NOT NULL DEFAULT 0, uncertain INTEGER NOT NULL DEFAULT 0,
    preimage_json TEXT NOT NULL DEFAULT '{}', postimage_json TEXT NOT NULL DEFAULT '{}',
    baseline_occurrences INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_repair_backup_slug ON repair_attempts(backup_slug) WHERE backup_slug != '';
CREATE INDEX IF NOT EXISTS idx_repair_attempts_target ON repair_attempts(target_key, created_at);
"""
_FAILED = ("failed", "verification_inconclusive", "interrupted", "rolled_back", "conflict", "backup_uncertain", "mutation_uncertain")


def target_key(repair_type: str, target: str) -> str:
    return hashlib.sha256(f"{repair_type}\0{target}".encode()).hexdigest()


def config_digest(config: dict[str, Any]) -> str:
    text = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


class RepairJournal:
    def __init__(self, path: str) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        with database_connection(self.path) as db:
            db.executescript(_SCHEMA)
            db.execute("INSERT OR IGNORE INTO repair_safety_meta VALUES ('owner', ?)", (secrets.token_hex(24),))

    async def claim(
        self, plan: dict[str, Any], target: str, mode: str, now: float,
        preimage: dict[str, Any], postimage: dict[str, Any],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._claim, plan, target, mode, now, preimage, postimage)

    def _claim(self, plan, target, mode, now, preimage, postimage) -> dict[str, Any]:
        key = target_key(plan["repair_type"], target)
        with database_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._check_claim(db, key, now)
            case = db.execute("SELECT * FROM incident_cases WHERE pattern_key=?", (plan["pattern_key"],)).fetchone()
            if not case or case["status"] in ("resolved", "historical", "suppressed_nonfatal"):
                raise RepairBlocked("case_is_not_active")
            changed = db.execute(
                "UPDATE repair_plans SET status='approved', approved_at=?, updated_at=? WHERE plan_id=? AND status='proposed'",
                (now, now, plan["plan_id"]),
            ).rowcount
            if changed != 1:
                raise RepairBlocked("repair_plan_already_claimed")
            execution_id = "exec_" + secrets.token_hex(10)
            owner = db.execute("SELECT value FROM repair_safety_meta WHERE key='owner'").fetchone()[0]
            db.execute(
                """INSERT INTO repair_attempts
                (execution_id,plan_id,target_key,target,repair_type,created_at,stage,nonce,owner,
                 preimage_json,postimage_json,baseline_occurrences)
                VALUES (?,?,?,?,?,?,'claimed',?,?,?,?,?)""",
                (execution_id, plan["plan_id"], key, target, plan["repair_type"], now,
                 secrets.token_hex(24), owner, json.dumps(preimage), json.dumps(postimage), case["occurrences"]),
            )
            db.execute(
                """INSERT INTO repair_executions
                (execution_id,plan_id,pattern_key,repair_type,target,started_at,baseline_last_seen,status,execution_mode)
                VALUES (?,?,?,?,?,?,?,'executing',?)""",
                (execution_id, plan["plan_id"], plan["pattern_key"], plan["repair_type"], target, now, case["last_seen"], mode),
            )
            db.execute("UPDATE incident_cases SET status='verifying', updated_at=? WHERE pattern_key=?", (now, plan["pattern_key"]))
            return dict(db.execute("SELECT * FROM repair_attempts WHERE execution_id=?", (execution_id,)).fetchone())

    @staticmethod
    def _check_claim(db: sqlite3.Connection, key: str, now: float) -> None:
        if db.execute("SELECT 1 FROM repair_attempts WHERE uncertain=1 LIMIT 1").fetchone():
            raise RepairBlocked("interrupted_or_uncertain_repair_needs_review")
        if db.execute("SELECT 1 FROM repair_executions WHERE status IN ('executing','verifying') LIMIT 1").fetchone():
            raise RepairBlocked("another_repair_is_active")
        rows = db.execute("SELECT stage,created_at FROM repair_attempts WHERE target_key=?", (key,)).fetchall()
        if any(row[0] in _FAILED for row in rows):
            raise RepairBlocked("target_has_failed_repair_needs_review")
        if any(now - float(row[1]) < 3600 for row in rows):
            raise RepairBlocked("target_repair_cooldown")

    @staticmethod
    def marker(row: dict[str, Any]) -> dict[str, str]:
        return {key: str(row[key]) for key in ("owner", "execution_id", "target_key", "nonce")}

    async def get(self, execution_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get, execution_id)

    def _get(self, execution_id: str) -> dict[str, Any] | None:
        with database_connection(self.path, readonly=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM repair_attempts WHERE execution_id=?", (execution_id,)).fetchone()
            return dict(row) if row else None

    async def advance(self, execution_id: str, stage: str, **fields: Any) -> None:
        await asyncio.to_thread(self._advance, execution_id, stage, fields)

    def _advance(self, execution_id: str, stage: str, fields: dict[str, Any]) -> None:
        allowed = {"backup_slug", "backup_job", "backup_size", "verification_started_at", "baseline_occurrences", "deleted", "protected", "uncertain", "error_code"}
        if set(fields) - allowed:
            raise ValueError("unsupported journal field")
        with database_connection(self.path) as db:
            db.execute(
                """UPDATE repair_attempts SET stage=?,
                backup_slug=COALESCE(?,backup_slug), backup_job=COALESCE(?,backup_job),
                backup_size=COALESCE(?,backup_size), verification_started_at=COALESCE(?,verification_started_at),
                baseline_occurrences=COALESCE(?,baseline_occurrences), deleted=COALESCE(?,deleted),
                protected=COALESCE(?,protected), uncertain=COALESCE(?,uncertain), error_code=COALESCE(?,error_code)
                WHERE execution_id=?""",
                (stage, *(fields.get(key) for key in ("backup_slug", "backup_job", "backup_size",
                  "verification_started_at", "baseline_occurrences", "deleted", "protected", "uncertain", "error_code")), execution_id),
            )
            if fields.get("deleted") == 1:
                db.execute("UPDATE repair_attempts SET preimage_json='{}',postimage_json='{}' WHERE execution_id=?", (execution_id,))

    async def claim_verification(self, execution_id: str) -> bool:
        return await asyncio.to_thread(self._claim_verification, execution_id)

    def _claim_verification(self, execution_id: str) -> bool:
        with database_connection(self.path) as db:
            return db.execute("UPDATE repair_attempts SET stage='checking' WHERE execution_id=? AND stage='verifying'", (execution_id,)).rowcount == 1

    async def backups(self, key: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._backups, key)

    def _backups(self, key: str | None) -> list[dict[str, Any]]:
        with database_connection(self.path, readonly=True) as db:
            db.row_factory = sqlite3.Row
            sql = "SELECT * FROM repair_attempts WHERE backup_slug != '' AND deleted=0"
            args: tuple[str, ...] = ()
            if key is not None:
                sql += " AND target_key=?"
                args = (key,)
            return [dict(row) for row in db.execute(sql + " ORDER BY created_at DESC, execution_id DESC", args)]

    async def recover_interrupted(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._recover)

    def _recover(self) -> list[dict[str, Any]]:
        with database_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("UPDATE repair_attempts SET stage='verifying' WHERE stage='checking'")
            rows = [dict(row) for row in db.execute("SELECT * FROM repair_attempts WHERE stage NOT IN ('succeeded','failed','withheld','verification_inconclusive','interrupted','rolled_back','conflict','backup_uncertain','mutation_uncertain','verifying')")]
            for row in rows:
                eid = row["execution_id"]
                db.execute("UPDATE repair_attempts SET stage='interrupted', uncertain=1, protected=1, error_code='interrupted_no_replay' WHERE execution_id=?", (eid,))
                db.execute("UPDATE repair_executions SET status='interrupted', error='interrupted_no_replay' WHERE execution_id=?", (eid,))
                db.execute("UPDATE repair_plans SET status='interrupted', error='interrupted_no_replay' WHERE plan_id=?", (row["plan_id"],))
                db.execute("UPDATE incident_cases SET status='needs_user_action' WHERE pattern_key=(SELECT pattern_key FROM repair_plans WHERE plan_id=?)", (row["plan_id"],))
            return rows

    async def health(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._health)

    def _health(self) -> dict[str, Any]:
        with database_connection(self.path, readonly=True) as db:
            rows = db.execute("SELECT stage,COUNT(*) FROM repair_attempts GROUP BY stage").fetchall()
            protected = db.execute("SELECT COUNT(*) FROM repair_attempts WHERE protected=1 AND backup_slug!='' AND deleted=0").fetchone()[0]
            uncertain = db.execute("SELECT COUNT(*) FROM repair_attempts WHERE uncertain=1").fetchone()[0]
            used = db.execute("SELECT COALESCE(SUM(backup_size),0) FROM repair_attempts WHERE deleted=0").fetchone()[0]
        return {"attempts_by_stage": dict(rows), "protected_backups": protected, "uncertain_attempts": uncertain, "recorded_backup_bytes": used}
