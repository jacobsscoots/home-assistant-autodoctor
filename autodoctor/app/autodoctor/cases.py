from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .models import Analysis, LogEvent

_CASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_cases (
    pattern_key TEXT PRIMARY KEY,
    pattern_label TEXT NOT NULL DEFAULT '',
    family TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'new',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 0,
    fingerprint_count INTEGER NOT NULL DEFAULT 0,
    representative_fingerprint TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    root_cause TEXT NOT NULL DEFAULT '',
    risk TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    repair_plan_id TEXT,
    notification_id TEXT NOT NULL,
    last_notification_at REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incident_cases_status_last_seen
ON incident_cases(status, last_seen DESC);

CREATE TABLE IF NOT EXISTS repair_plans (
    plan_id TEXT PRIMARY KEY,
    pattern_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    risk TEXT NOT NULL DEFAULT 'high',
    confidence REAL NOT NULL DEFAULT 0,
    repair_type TEXT NOT NULL DEFAULT 'manual_review',
    summary TEXT NOT NULL DEFAULT '',
    root_cause TEXT NOT NULL DEFAULT '',
    proposed_changes_json TEXT NOT NULL DEFAULT '[]',
    checks_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    approved_at REAL,
    executed_at REAL,
    verified_at REAL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_repair_plans_pattern_created
ON repair_plans(pattern_key, created_at DESC);
"""

_BACKLOG_ACTIVE_WINDOW_SECONDS = 24 * 3600
_ACTIVE_STATUSES = {
    "new",
    "investigating",
    "diagnosed",
    "repair_available",
    "needs_user_action",
    "verifying",
    "reopened",
}


class IncidentCaseManager:
    """Pattern-level incident lifecycle and notification ownership.

    The exact-fingerprint incident table remains the evidence ledger. This layer groups
    related fingerprints into one operational case, one stable Home Assistant persistent
    notification, and at most one current repair plan. Repair plans are proposals only in
    v0.3.0; this module deliberately contains no Home Assistant mutation executor.
    """

    def __init__(self, db_path: str, ha: Any, *, notifications_enabled: bool = True) -> None:
        self.db_path = db_path
        self.ha = ha
        self.notifications_enabled = bool(notifications_enabled)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.executescript(_CASE_SCHEMA)
            db.commit()

    @staticmethod
    def notification_id(pattern_key: str) -> str:
        digest = hashlib.sha256(pattern_key.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"autodoctor_case_{digest}"

    @staticmethod
    def _now() -> float:
        return datetime.now(tz=timezone.utc).timestamp()

    async def record_event(
        self,
        *,
        pattern_key: str,
        pattern_label: str,
        family: str,
        fingerprint: str,
        event: LogEvent,
        fingerprint_is_new: bool,
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            case, is_new_case = await asyncio.to_thread(
                self._record_event_sync,
                pattern_key,
                pattern_label,
                family,
                fingerprint,
                event.timestamp,
                fingerprint_is_new,
            )
        return case, is_new_case

    def _record_event_sync(
        self,
        pattern_key: str,
        pattern_label: str,
        family: str,
        fingerprint: str,
        timestamp: float,
        fingerprint_is_new: bool,
    ) -> tuple[dict[str, Any], bool]:
        key = str(pattern_key or f"fingerprint/{fingerprint}")
        now = float(timestamp)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            existing = db.execute(
                "SELECT * FROM incident_cases WHERE pattern_key = ?", (key,)
            ).fetchone()
            is_new_case = existing is None
            if is_new_case:
                db.execute(
                    """INSERT INTO incident_cases
                    (pattern_key, pattern_label, family, status, first_seen, last_seen,
                     occurrences, fingerprint_count, representative_fingerprint,
                     notification_id, updated_at)
                    VALUES (?, ?, ?, 'new', ?, ?, 1, 1, ?, ?, ?)""",
                    (
                        key,
                        str(pattern_label or key),
                        str(family or "unknown"),
                        now,
                        now,
                        fingerprint,
                        self.notification_id(key),
                        now,
                    ),
                )
            else:
                status = str(existing["status"] or "new")
                if status in {"resolved", "historical"}:
                    status = "reopened"
                db.execute(
                    """UPDATE incident_cases SET
                        pattern_label = ?, family = ?, status = ?, last_seen = ?,
                        occurrences = occurrences + 1,
                        fingerprint_count = fingerprint_count + ?,
                        representative_fingerprint = ?, updated_at = ?
                    WHERE pattern_key = ?""",
                    (
                        str(pattern_label or key),
                        str(family or "unknown"),
                        status,
                        now,
                        1 if fingerprint_is_new else 0,
                        fingerprint,
                        now,
                        key,
                    ),
                )
            db.commit()
            row = db.execute(
                "SELECT * FROM incident_cases WHERE pattern_key = ?", (key,)
            ).fetchone()
            return dict(row), is_new_case

    async def reconcile_backlog(self, incident_rows: list[dict[str, Any]]) -> dict[str, int]:
        """Group historical exact incidents and retire only AutoDoctor's legacy notices."""
        groups: dict[str, dict[str, Any]] = {}
        dismissed = 0
        for row in incident_rows:
            fp = str(row.get("fingerprint") or "")
            key = str(row.get("pattern_key") or f"fingerprint/{fp}")
            row_first = float(row.get("first_seen") or 0)
            row_last = float(row.get("last_seen") or 0)
            item = groups.setdefault(
                key,
                {
                    "pattern_key": key,
                    "pattern_label": str(row.get("pattern_label") or key),
                    "family": "unknown",
                    "first_seen": row_first,
                    "last_seen": row_last,
                    "occurrences": 0,
                    "fingerprints": 0,
                    "representative_fingerprint": fp,
                },
            )
            if row_first:
                item["first_seen"] = min(float(item["first_seen"] or row_first), row_first)
            if row_last >= float(item["last_seen"] or 0):
                item["last_seen"] = row_last
                item["representative_fingerprint"] = fp
            item["occurrences"] += int(row.get("occurrences") or 0)
            item["fingerprints"] += 1
            if fp and self.notifications_enabled:
                try:
                    await self.ha.dismiss_notification(f"autodoctor_{fp}")
                    dismissed += 1
                except Exception:
                    # Backlog cleanup must never stop incident monitoring.
                    pass

        async with self._lock:
            await asyncio.to_thread(self._merge_backlog_sync, list(groups.values()))

        if self.notifications_enabled:
            for key in groups:
                await self.publish_case(key, force=True)
        return {"cases": len(groups), "legacy_notifications_dismissed": dismissed}

    def _merge_backlog_sync(self, groups: list[dict[str, Any]]) -> None:
        now = self._now()
        active_cutoff = now - _BACKLOG_ACTIVE_WINDOW_SECONDS
        with sqlite3.connect(self.db_path) as db:
            for item in groups:
                existing = db.execute(
                    "SELECT status FROM incident_cases WHERE pattern_key = ?",
                    (item["pattern_key"],),
                ).fetchone()
                if existing:
                    status = str(existing[0])
                else:
                    status = "new" if float(item["last_seen"] or 0) >= active_cutoff else "historical"
                db.execute(
                    """INSERT INTO incident_cases
                    (pattern_key, pattern_label, family, status, first_seen, last_seen,
                     occurrences, fingerprint_count, representative_fingerprint,
                     notification_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pattern_key) DO UPDATE SET
                        pattern_label=excluded.pattern_label,
                        first_seen=MIN(incident_cases.first_seen, excluded.first_seen),
                        last_seen=MAX(incident_cases.last_seen, excluded.last_seen),
                        occurrences=MAX(incident_cases.occurrences, excluded.occurrences),
                        fingerprint_count=MAX(incident_cases.fingerprint_count, excluded.fingerprint_count),
                        representative_fingerprint=excluded.representative_fingerprint,
                        updated_at=excluded.updated_at""",
                    (
                        item["pattern_key"],
                        item["pattern_label"],
                        item["family"],
                        status,
                        item["first_seen"],
                        item["last_seen"],
                        item["occurrences"],
                        item["fingerprints"],
                        item["representative_fingerprint"],
                        self.notification_id(item["pattern_key"]),
                        now,
                    ),
                )
            db.commit()

    async def mark_investigating(self, pattern_key: str) -> None:
        await self._set_status(pattern_key, "investigating")
        await self.publish_case(pattern_key, force=True)

    async def mark_needs_user_action(self, pattern_key: str, summary: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_case_summary_sync,
                pattern_key,
                "needs_user_action",
                str(summary)[:2000],
                "",
                "",
                0.0,
                None,
            )
        await self.publish_case(pattern_key, force=True)

    async def apply_analysis(
        self,
        *,
        pattern_key: str,
        fingerprint: str,
        analysis: Analysis,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        plan = None
        status = "diagnosed"
        if analysis.action == "propose_fix" and analysis.proposed_changes:
            plan = self._repair_plan_payload(pattern_key, fingerprint, analysis, evidence or {})
            status = "repair_available"
            async with self._lock:
                await asyncio.to_thread(self._save_plan_sync, plan)
        async with self._lock:
            await asyncio.to_thread(
                self._update_case_summary_sync,
                pattern_key,
                status,
                analysis.summary,
                analysis.root_cause,
                analysis.risk,
                analysis.confidence,
                plan["plan_id"] if plan else None,
            )
        await self.publish_case(pattern_key, force=True)
        return plan

    @staticmethod
    def _repair_type(analysis: Analysis) -> str:
        # v0.3.0 never executes these. Classification exists only so the later executor
        # can require an exact supported type instead of interpreting free-form AI prose.
        operations = {
            str(change.get("operation") or change.get("action") or "").strip().lower()
            for change in analysis.proposed_changes
            if isinstance(change, dict)
        }
        if operations & {"reload_config_entry", "reload_integration"}:
            return "reload_config_entry"
        return "manual_review"

    def _repair_plan_payload(
        self,
        pattern_key: str,
        fingerprint: str,
        analysis: Analysis,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        now = self._now()
        material = f"{pattern_key}|{fingerprint}|{now:.6f}"
        plan_id = "plan_" + hashlib.sha256(material.encode()).hexdigest()[:20]
        return {
            "plan_id": plan_id,
            "pattern_key": pattern_key,
            "fingerprint": fingerprint,
            "created_at": now,
            "updated_at": now,
            "status": "proposed",
            "risk": analysis.risk,
            "confidence": float(analysis.confidence),
            "repair_type": self._repair_type(analysis),
            "summary": str(analysis.summary)[:3000],
            "root_cause": str(analysis.root_cause)[:5000],
            "proposed_changes": analysis.proposed_changes[:20],
            "checks": analysis.checks[:30],
            "evidence": evidence,
        }

    def _save_plan_sync(self, plan: dict[str, Any]) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """INSERT INTO repair_plans
                (plan_id, pattern_key, fingerprint, created_at, updated_at, status,
                 risk, confidence, repair_type, summary, root_cause,
                 proposed_changes_json, checks_json, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan["plan_id"],
                    plan["pattern_key"],
                    plan["fingerprint"],
                    plan["created_at"],
                    plan["updated_at"],
                    plan["status"],
                    plan["risk"],
                    plan["confidence"],
                    plan["repair_type"],
                    plan["summary"],
                    plan["root_cause"],
                    json.dumps(plan["proposed_changes"], separators=(",", ":")),
                    json.dumps(plan["checks"], separators=(",", ":")),
                    json.dumps(plan["evidence"], separators=(",", ":")),
                ),
            )
            db.commit()

    async def _set_status(self, pattern_key: str, status: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_status_sync, pattern_key, status)

    def _set_status_sync(self, pattern_key: str, status: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE incident_cases SET status = ?, updated_at = ? WHERE pattern_key = ?",
                (status, self._now(), pattern_key),
            )
            db.commit()

    def _update_case_summary_sync(
        self,
        pattern_key: str,
        status: str,
        summary: str,
        root_cause: str,
        risk: str,
        confidence: float,
        repair_plan_id: str | None,
    ) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """UPDATE incident_cases SET status = ?, summary = ?, root_cause = ?,
                   risk = ?, confidence = ?, repair_plan_id = ?, updated_at = ?
                   WHERE pattern_key = ?""",
                (
                    status,
                    str(summary)[:3000],
                    str(root_cause)[:5000],
                    str(risk)[:20],
                    float(confidence),
                    repair_plan_id,
                    self._now(),
                    pattern_key,
                ),
            )
            db.commit()

    async def get_case(self, pattern_key: str) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_case_sync, pattern_key)

    def _get_case_sync(self, pattern_key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM incident_cases WHERE pattern_key = ?", (pattern_key,)
            ).fetchone()
            return dict(row) if row else None

    async def list_cases(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_cases_sync, limit)

    def _list_cases_sync(self, limit: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM incident_cases ORDER BY last_seen DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [dict(row) for row in rows]

    async def list_repair_plans(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_repair_plans_sync, limit)

    def _list_repair_plans_sync(self, limit: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM repair_plans ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                for field in ("proposed_changes_json", "checks_json", "evidence_json"):
                    try:
                        item[field.removesuffix("_json")] = json.loads(item.pop(field))
                    except (TypeError, json.JSONDecodeError):
                        item[field.removesuffix("_json")] = [] if field != "evidence_json" else {}
                result.append(item)
            return result

    @staticmethod
    def _notification_message(case: dict[str, Any]) -> str:
        lines = [
            f"Status: {case.get('status', 'new')}",
            f"Occurrences: {int(case.get('occurrences') or 0)} across {int(case.get('fingerprint_count') or 0)} exact fingerprint(s)",
        ]
        summary = str(case.get("summary") or "")
        if summary:
            lines.append(f"Diagnosis: {summary[:800]}")
        if case.get("repair_plan_id"):
            lines.append("Repair plan: available for review; no change has been executed.")
        lines.append("AutoDoctor groups related incidents into this single case notification.")
        return "\n\n".join(lines)

    async def publish_case(self, pattern_key: str, *, force: bool = False) -> bool:
        if not self.notifications_enabled:
            return False
        case = await self.get_case(pattern_key)
        if not case or str(case.get("status")) not in _ACTIVE_STATUSES:
            return False
        now = self._now()
        last = float(case.get("last_notification_at") or 0)
        occurrences = int(case.get("occurrences") or 0)
        milestone = occurrences in {1, 2, 5, 10, 25, 50, 100} or (occurrences > 100 and occurrences % 100 == 0)
        if not force and not milestone and now - last < 900:
            return False
        await self.ha.notify(
            f"AutoDoctor: {str(case.get('pattern_label') or pattern_key)[:160]}",
            self._notification_message(case),
            str(case["notification_id"]),
        )
        async with self._lock:
            await asyncio.to_thread(self._mark_notified_sync, pattern_key, now)
        return True

    def _mark_notified_sync(self, pattern_key: str, now: float) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE incident_cases SET last_notification_at = ? WHERE pattern_key = ?",
                (now, pattern_key),
            )
            db.commit()

    async def mark_resolved(self, pattern_key: str, *, verification: str = "") -> None:
        case = await self.get_case(pattern_key)
        if not case:
            return
        await self._set_status(pattern_key, "resolved")
        if self.notifications_enabled:
            await self.ha.dismiss_notification(str(case["notification_id"]))

    async def health(self) -> dict[str, Any]:
        cases = await self.list_cases(500)
        plans = await self.list_repair_plans(500)
        statuses: dict[str, int] = {}
        for case in cases:
            status = str(case.get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        return {
            "cases_total": len(cases),
            "cases_by_status": statuses,
            "repair_plans_total": len(plans),
            "repair_plans_proposed": sum(1 for plan in plans if plan.get("status") == "proposed"),
            "executor_enabled": False,
        }
