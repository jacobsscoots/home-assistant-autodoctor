from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from .cases import IncidentCaseManager

_LOG = logging.getLogger(__name__)

_ACTIVE_NOTIFICATION_STATUSES = {
    "new",
    "investigating",
    "diagnosed",
    "repair_available",
    "needs_user_action",
    "verifying",
    "reopened",
}
_INACTIVE_NOTIFICATION_STATUSES = {"resolved", "historical", "suppressed_nonfatal"}
_SUPPRESSION_PROTECTED_STATUSES = {
    "investigating",
    "repair_available",
    "needs_user_action",
    "verifying",
}
_DEFAULT_QUIET_RETIRE_SECONDS = 24 * 3600


class LifecycleIncidentCaseManager(IncidentCaseManager):
    """Case manager with explicit persistent-notification ownership semantics.

    Active cases may own one stable AutoDoctor notification. Inactive cases never do.
    Notification dismissal is idempotent, limited to IDs already stored on AutoDoctor
    cases, and failures are retried by later reconciliation rather than affecting log
    ingestion.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.notification_dismissals = 0
        self.notification_dismiss_failures = 0
        self.quiet_cases_retired = 0
        self.nonfatal_cases_suppressed = 0
        self._inactive_notification_reconciliations = 0

    async def reconcile_backlog(
        self,
        incident_rows: list[dict[str, Any]],
        *,
        publish_active: bool = True,
    ) -> dict[str, int]:
        """Reconcile exact incidents without forcing active notices before policy runs."""

        groups: dict[str, dict[str, Any]] = {}
        dismissed = 0
        for row in incident_rows:
            fingerprint = self._aggregate_backlog_row(groups, row)
            dismissed += int(await self._dismiss_legacy_notification(fingerprint))

        async with self._lock:
            await asyncio.to_thread(self._merge_backlog_sync, list(groups.values()))

        published = await self.publish_active_cases(force=True) if publish_active else 0
        return {
            "cases": len(groups),
            "legacy_notifications_dismissed": dismissed,
            "active_case_notifications_published": published,
        }

    async def publish_active_cases(self, *, force: bool = False) -> int:
        published = 0
        for case in await self.list_cases(500):
            if str(case.get("status") or "") not in _ACTIVE_NOTIFICATION_STATUSES:
                continue
            published += int(await self.publish_case(str(case.get("pattern_key") or ""), force=force))
        return published

    async def _dismiss_owned_notification(
        self,
        pattern_key: str,
        case: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """Dismiss one deterministic AutoDoctor case notice.

        Normal lifecycle paths use the durable ``last_notification_at`` marker so a
        successfully dismissed notice is not called repeatedly. Startup reconciliation
        may set ``force=True`` once to close the tiny crash window where Home Assistant
        accepted notification creation but the process stopped before persisting that
        marker. The ID is still taken only from AutoDoctor's own case row and must be in
        the AutoDoctor namespace.
        """

        if not self.notifications_enabled:
            return False
        current = case or await self.get_case(pattern_key)
        if not current:
            return False
        if not force and current.get("last_notification_at") is None:
            return False
        notification_id = str(current.get("notification_id") or "")
        if not notification_id.startswith("autodoctor_case_"):
            _LOG.warning("Refusing to dismiss notification outside AutoDoctor case namespace")
            return False
        try:
            await self.ha.dismiss_notification(notification_id)
        except Exception as exc:
            self.notification_dismiss_failures += 1
            _LOG.warning("Could not dismiss AutoDoctor case notification safely: %s", exc)
            return False
        async with self._lock:
            await asyncio.to_thread(self._clear_notification_marker_sync, pattern_key)
        self.notification_dismissals += 1
        return True

    def _clear_notification_marker_sync(self, pattern_key: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE incident_cases SET last_notification_at = NULL WHERE pattern_key = ?",
                (pattern_key,),
            )
            db.commit()

    async def reconcile_inactive_notifications(self, *, force: bool = False) -> int:
        """Dismiss stale AutoDoctor notices owned by inactive cases.

        The first reconciliation after process start is always forced. This closes the
        crash window where Home Assistant accepted a case notification but AutoDoctor
        stopped before persisting ``last_notification_at``. Later maintenance passes
        use the marker and therefore do not repeatedly dismiss already-clean notices.
        """

        startup_force = self._inactive_notification_reconciliations == 0
        self._inactive_notification_reconciliations += 1
        effective_force = bool(force or startup_force)
        dismissed = 0
        for case in await self.list_cases(500):
            if str(case.get("status") or "") not in _INACTIVE_NOTIFICATION_STATUSES:
                continue
            dismissed += int(
                await self._dismiss_owned_notification(
                    str(case.get("pattern_key") or ""),
                    case,
                    force=effective_force,
                )
            )
        return dismissed

    async def mark_suppressed_nonfatal(self, pattern_key: str, reason: str) -> bool:
        """Retain a proven non-fatal observation without AI, repair, or notification."""

        case = await self.get_case(pattern_key)
        if not case:
            return False
        status = str(case.get("status") or "")
        if status == "suppressed_nonfatal" or status in _SUPPRESSION_PROTECTED_STATUSES:
            return False
        async with self._lock:
            changed = await asyncio.to_thread(
                self._mark_suppressed_nonfatal_sync,
                pattern_key,
                str(reason)[:2000],
            )
        if not changed:
            return False
        await self._dismiss_owned_notification(pattern_key)
        self.nonfatal_cases_suppressed += 1
        return True

    def _mark_suppressed_nonfatal_sync(self, pattern_key: str, reason: str) -> bool:
        with sqlite3.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM incident_cases WHERE pattern_key = ?",
                (pattern_key,),
            ).fetchone()
            status = str(row[0] or "") if row else ""
            if not row or status == "suppressed_nonfatal" or status in _SUPPRESSION_PROTECTED_STATUSES:
                db.rollback()
                return False
            db.execute(
                """UPDATE incident_cases
                SET status = 'suppressed_nonfatal', summary = ?, root_cause = ?,
                    risk = 'low', confidence = 1.0, repair_plan_id = NULL, updated_at = ?
                WHERE pattern_key = ?""",
                (
                    reason,
                    "The raw library sub-call did not escalate to a Home Assistant coordinator failure.",
                    self._now(),
                    pattern_key,
                ),
            )
            db.commit()
            return True

    async def reopen_if_suppressed(self, pattern_key: str) -> bool:
        """Fail open to investigation if a later event no longer matches suppression policy."""

        async with self._lock:
            return await asyncio.to_thread(self._reopen_if_suppressed_sync, pattern_key)

    def _reopen_if_suppressed_sync(self, pattern_key: str) -> bool:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status FROM incident_cases WHERE pattern_key = ?",
                (pattern_key,),
            ).fetchone()
            if not row or str(row[0] or "") != "suppressed_nonfatal":
                return False
            db.execute(
                """UPDATE incident_cases
                SET status = 'reopened', summary = '', root_cause = '', risk = '',
                    confidence = 0, repair_plan_id = NULL, updated_at = ?
                WHERE pattern_key = ?""",
                (self._now(), pattern_key),
            )
            db.commit()
            return True

    async def retire_quiet_cases(
        self,
        *,
        quiet_seconds: int = _DEFAULT_QUIET_RETIRE_SECONDS,
        now: float | None = None,
    ) -> int:
        """Move unattended quiet cases to historical and dismiss their notices.

        Quiet retirement deliberately excludes cases awaiting user action, repair
        approval, active investigation, or verification. A later recurrence reopens a
        historical case through the existing case ingestion path.
        """

        current = float(self._now() if now is None else now)
        cutoff = current - max(3600, int(quiet_seconds))
        async with self._lock:
            retired = await asyncio.to_thread(self._retire_quiet_cases_sync, cutoff, current)
        for pattern_key in retired:
            await self._dismiss_owned_notification(pattern_key)
        self.quiet_cases_retired += len(retired)
        return len(retired)

    @staticmethod
    def _quiet_case_rows(db: sqlite3.Connection, cutoff: float) -> list[str]:
        rows = db.execute(
            """SELECT pattern_key FROM incident_cases
            WHERE status IN ('new', 'diagnosed', 'reopened') AND last_seen < ?""",
            (cutoff,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _retire_quiet_cases_sync(self, cutoff: float, now: float) -> list[str]:
        with sqlite3.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            keys = self._quiet_case_rows(db, cutoff)
            retired: list[str] = []
            for pattern_key in keys:
                cursor = db.execute(
                    """UPDATE incident_cases
                    SET status = 'historical', updated_at = ?
                    WHERE pattern_key = ? AND status IN ('new', 'diagnosed', 'reopened')""",
                    (now, pattern_key),
                )
                if cursor.rowcount == 1:
                    retired.append(pattern_key)
            db.commit()
            return retired

    async def mark_resolved(self, pattern_key: str, *, verification: str = "") -> None:
        case = await self.get_case(pattern_key)
        if not case:
            return
        await self._set_status(pattern_key, "resolved")
        await self._dismiss_owned_notification(pattern_key, case)

    async def lifecycle_health(self) -> dict[str, Any]:
        return {
            "notification_policy": "active-cases-only",
            "inactive_statuses": sorted(_INACTIVE_NOTIFICATION_STATUSES),
            "quiet_retire_seconds": _DEFAULT_QUIET_RETIRE_SECONDS,
            "notification_dismissals": self.notification_dismissals,
            "notification_dismiss_failures": self.notification_dismiss_failures,
            "quiet_cases_retired": self.quiet_cases_retired,
            "nonfatal_cases_suppressed": self.nonfatal_cases_suppressed,
        }
