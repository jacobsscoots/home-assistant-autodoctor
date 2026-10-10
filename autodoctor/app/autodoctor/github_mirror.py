"""Opt-in, structured-only GitHub history with a durable outbox and no POST replay."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from . import AUTODOCTOR_VERSION
from .database import database_connection

_LOG = logging.getLogger(__name__)
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{20}$")
_LABELS = frozenset({"device_query_timeout", "timeout", "connection_refused", "rate_limit", "authentication",
                    "template_error", "not_found", "unavailable", "storage", "parse_error", "other",
                    "missing_entity", "unavailable_entity", "stale_entity", "disabled_integration",
                    "integration_setup_error", "integration_setup_retry", "integration_not_loaded"})
_STATUSES = frozenset({"new", "investigating", "diagnosed", "repair_available", "needs_user_action", "verifying",
                      "reopened", "resolved", "historical", "suppressed_nonfatal"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS github_history_outbox (
    repository TEXT NOT NULL, case_key TEXT NOT NULL, issue_number INTEGER, title TEXT NOT NULL,
    desired_body TEXT NOT NULL, desired_state TEXT NOT NULL, desired_hash TEXT NOT NULL,
    applied_hash TEXT NOT NULL DEFAULT '', create_pending INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (repository, case_key)
);
"""


class GitHubRequestRejected(RuntimeError):
    """A definitive non-write response; unlike a timeout/5xx it permits a create retry."""

    def __init__(self, status: int) -> None:
        super().__init__("github_request_rejected")
        self.status = status


def structured_history(case: dict, verified: bool) -> tuple[str, str, str] | None:
    """No raw symptom, AI prose, entities, family names or configuration can leave HA."""
    label, status = case.get("pattern_label"), case.get("status")
    fp = str(case.get("representative_fingerprint") or "")
    if label not in _LABELS or status not in _STATUSES or not _FINGERPRINT.fullmatch(fp):
        return None
    if status == "suppressed_nonfatal":
        return None
    try:
        dates = [float(case[x]) for x in ("first_seen", "last_seen")]
        count = int(case["occurrences"])
        if not all(math.isfinite(x) and x > 0 for x in dates) or count < 1:
            return None
        first, last = [datetime.fromtimestamp(x, timezone.utc).isoformat() for x in dates]
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    key = hashlib.sha256(str(case["pattern_key"]).encode()).hexdigest()
    fields = {"fingerprint": fp, "first_seen_utc": first, "last_seen_utc": last,
              "occurrences": count, "failure_class": label, "case_status": status,
              "autodoctor_version": AUTODOCTOR_VERSION,
              "source": "autodoctor-repair" if verified else "autodoctor-diagnosis-only",
              "verified_autodoctor_repair": verified}
    text = "Verified backed-up repair." if verified else "Observation/diagnosis only. No verified AutoDoctor repair is claimed."
    body = (f"<!-- autodoctor-case:{key} -->\n\n{text}\n\n```json\n"
            + json.dumps(fields, indent=2, sort_keys=True) + "\n```\n\n"
            "Detailed evidence and diagnosis remain in the authenticated Home Assistant dashboard. "
            "Free-text symptoms and AI recommendations are intentionally not exported.\n")
    return "HA incident: " + label + " [" + key[:12] + "]", body, "closed" if verified else "open"


class GitHubHistoryMirror:
    def __init__(self, settings: Any, db_path: str, *, clock=time.time) -> None:
        self.settings, self.path, self.clock = settings, db_path, clock
        self.enabled = bool(settings.github_history_enabled)
        self.configured = bool(_REPOSITORY.fullmatch(settings.github_history_repository) and settings.github_history_token.strip())
        self.cursor = (0.0, "")
        self.last_result = "disabled" if not self.enabled else "waiting_for_first_scan"
        self.synced = 0
        self.queue_status = {"pending_updates": 0, "uncertain_creates": 0}
        self.runtime = None

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        with database_connection(self.path) as db:
            db.executescript(_SCHEMA)

    @staticmethod
    def _verified(db, case: dict) -> bool:
        if case.get("status") != "resolved" or not case.get("repair_plan_id"):
            return False
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"repair_plans", "repair_attempts", "repair_executions"} <= tables:
            return False
        return db.execute(
            "SELECT 1 FROM repair_plans p JOIN repair_attempts a ON a.plan_id=p.plan_id "
            "JOIN repair_executions e ON e.execution_id=a.execution_id WHERE p.plan_id=? "
            "AND p.status='succeeded' AND a.stage='succeeded' AND e.status='succeeded' "
            "AND a.uncertain=0 AND a.backup_slug!='' AND a.verification_started_at IS NOT NULL LIMIT 1",
            (case["repair_plan_id"],),
        ).fetchone() is not None

    def _enqueue(self) -> None:
        cursor = self.cursor
        with database_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM incident_cases WHERE updated_at>? OR (updated_at=? AND pattern_key>?) ORDER BY updated_at,pattern_key LIMIT 100",
                              (self.cursor[0], self.cursor[0], self.cursor[1])).fetchall()
            for row in rows:
                case = dict(row)
                cursor = (case["updated_at"], case["pattern_key"])
                payload = structured_history(case, self._verified(db, case))
                if not payload:
                    continue
                title, body, state = payload
                key = hashlib.sha256(case["pattern_key"].encode()).hexdigest()
                digest = hashlib.sha256((title + body + state).encode()).hexdigest()
                db.execute("INSERT INTO github_history_outbox (repository,case_key,title,desired_body,desired_state,desired_hash) VALUES (?,?,?,?,?,?) ON CONFLICT(repository,case_key) DO UPDATE SET title=excluded.title,desired_body=excluded.desired_body,desired_state=excluded.desired_state,desired_hash=excluded.desired_hash",
                           (self.settings.github_history_repository, key, title, body, state, digest))
        self.cursor = cursor  # Advance only after the transaction commits.

    def _next(self) -> dict | None:
        with database_connection(self.path, readonly=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM github_history_outbox WHERE repository=? AND desired_hash!=applied_hash AND next_attempt_at<=? ORDER BY next_attempt_at,case_key LIMIT 1", (self.settings.github_history_repository, self.clock())).fetchone()
            return dict(row) if row else None

    async def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        # Separate session: never forward the Home Assistant Supervisor credential.
        headers = {"Authorization": "Bearer " + self.settings.github_history_token,
                   "User-Agent": "HomeAssistant-AutoDoctor/" + AUTODOCTOR_VERSION,
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.request(method, "https://api.github.com" + path, json=payload, allow_redirects=False) as response:
                if response.status in {400, 401, 403, 404, 422, 429}:
                    raise GitHubRequestRejected(response.status)
                raw = bytearray()
                while chunk := await response.content.read(16384):
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise RuntimeError("github_response_bound_reached")
                if response.status not in {200, 201} or len(raw) > 2 * 1024 * 1024:
                    raise RuntimeError("github_request_unavailable")
                return json.loads(raw)

    async def _find_issue(self, key: str) -> int | None:
        marker = "<!-- autodoctor-case:" + key + " -->"
        found = []
        repo = self.settings.github_history_repository
        for page in range(1, 6):
            issues = await self._request("GET", f"/repos/{repo}/issues?state=all&per_page=100&page={page}")
            if not isinstance(issues, list):
                raise RuntimeError("invalid_github_inventory")
            for item in issues:
                if not item.get("pull_request") and marker in str(item.get("body") or ""):
                    found.append(int(item["number"]))
            if len(issues) < 100:
                if len(found) > 1:
                    raise RuntimeError("ambiguous_github_marker")
                return found[0] if found else None
        raise RuntimeError("github_inventory_bound_reached")  # Never create after an incomplete search.

    def _set_pending(self, key: str) -> None:
        with database_connection(self.path) as db:
            db.execute("UPDATE github_history_outbox SET create_pending=1 WHERE repository=? AND case_key=?", (self.settings.github_history_repository, key))

    def _clear_pending(self, key: str) -> None:
        with database_connection(self.path) as db:
            db.execute("UPDATE github_history_outbox SET create_pending=0 WHERE repository=? AND case_key=?", (self.settings.github_history_repository, key))

    def _queue_counts(self) -> dict[str, int]:
        with database_connection(self.path, readonly=True) as db:
            row = db.execute("SELECT COUNT(*),COALESCE(SUM(create_pending),0) FROM github_history_outbox WHERE repository=? AND desired_hash!=applied_hash", (self.settings.github_history_repository,)).fetchone()
            return {"pending_updates": row[0], "uncertain_creates": row[1]}

    def _record_issue(self, key: str, number: int) -> None:
        if number < 1:
            raise ValueError("invalid issue number")
        with database_connection(self.path) as db:
            db.execute("UPDATE github_history_outbox SET issue_number=?,create_pending=0 WHERE repository=? AND case_key=?", (number, self.settings.github_history_repository, key))

    def _complete(self, item: dict) -> None:
        with database_connection(self.path) as db:
            db.execute("UPDATE github_history_outbox SET applied_hash=?,attempts=0,next_attempt_at=0 WHERE repository=? AND case_key=?",
                       (item["desired_hash"], self.settings.github_history_repository, item["case_key"]))

    def _defer(self, item: dict) -> None:
        delay = min(3600, 60 * 2 ** min(6, int(item["attempts"])))
        with database_connection(self.path) as db:
            db.execute("UPDATE github_history_outbox SET attempts=attempts+1,next_attempt_at=? WHERE repository=? AND case_key=?",
                       (self.clock() + delay, self.settings.github_history_repository, item["case_key"]))

    async def run_once(self) -> None:
        if not self.enabled:
            return
        if not self.configured:
            self.last_result = "dedicated_repository_credential_required"
            return
        await asyncio.to_thread(self._enqueue)
        item = await asyncio.to_thread(self._next)
        if not item:
            self.last_result = "queue_current"
            return
        repo, key = self.settings.github_history_repository, item["case_key"]
        try:
            number = item["issue_number"]
            if number is None:
                number = await self._find_issue(key)
                if number is None and item["create_pending"]:
                    raise RuntimeError("uncertain_create_needs_reconciliation")
                if number is None:
                    await asyncio.to_thread(self._set_pending, key)
                    try:
                        result = await self._request("POST", f"/repos/{repo}/issues", {"title": item["title"], "body": item["desired_body"]})
                    except GitHubRequestRejected:
                        await asyncio.to_thread(self._clear_pending, key)
                        raise
                    number = int(result["number"])
                await asyncio.to_thread(self._record_issue, key, number)
            remote = await self._request("GET", f"/repos/{repo}/issues/{number}")
            if remote.get("pull_request") or "<!-- autodoctor-case:" + key + " -->" not in str(remote.get("body") or ""):
                raise RuntimeError("github_issue_marker_changed")
            await self._request("PATCH", f"/repos/{repo}/issues/{number}",
                                {"title": item["title"], "body": item["desired_body"], "state": item["desired_state"]})
            await asyncio.to_thread(self._complete, item)
            self.synced += 1
            self.last_result = "history_synced"
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.to_thread(self._defer, item)
            self.last_result = "history_deferred_no_create_replay"
        finally:
            self.queue_status = await asyncio.to_thread(self._queue_counts)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_result = "local_history_queue_unavailable"
                _LOG.warning("GitHub history queue unavailable; local monitoring continues")
            if self.runtime:
                self.runtime.beat("github-history")
            await asyncio.sleep(60)

    def health(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "configured": self.configured,
                "synced_updates": self.synced, "last_result": self.last_result, **self.queue_status}
