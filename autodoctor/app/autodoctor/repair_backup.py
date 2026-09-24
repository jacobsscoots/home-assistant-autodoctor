"""Restricted Supervisor backup transport; no restore or arbitrary API surface."""
from __future__ import annotations

import math
import re
from typing import Any

import aiohttp

MIB = 1024 * 1024
_LOCAL_BACKUP_LOCATION = ".local"
_CREATE_BACKUP_PATH = "/backups/new/partial"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class RepairBlocked(RuntimeError):
    """A public, non-sensitive reason why a repair cannot proceed."""


class BackupUncertain(RepairBlocked):
    """A backup request may have completed; never repeat it automatically."""


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RepairBlocked("invalid_resource_identifier")
    return value


def positive_size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RepairBlocked("backup_size_unavailable")
    if not math.isfinite(value) or value <= 0:
        raise RepairBlocked("backup_size_unavailable")
    return int(value)


def job_succeeded(job: dict[str, Any]) -> bool:
    """Require completion of every child, not merely a returned job ID."""
    if job.get("done") is not True or job.get("errors") or job.get("error"):
        return False
    children = job.get("child_jobs", [])
    return isinstance(children, list) and all(
        isinstance(child, dict) and job_succeeded(child) for child in children
    )


def _all_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pending = list(jobs)
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            raise RepairBlocked("invalid_job_evidence")
        result.append(item)
        children = item.get("child_jobs", [])
        if not isinstance(children, list):
            raise RepairBlocked("invalid_job_evidence")
        pending.extend(children)
        if len(result) > 10000:
            raise RepairBlocked("job_evidence_too_large")
    return result


class SupervisorBackupClient:
    """Only backup create/info/delete, host space and job-info requests.

    Uses the existing authenticated session. Never logs response bodies, names,
    passwords, or URLs containing resource identifiers. POSTs are not retried.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Backup creation has a longer deadline; other fixed Supervisor routes use 30 seconds.
        request_timeout = 600 if (method, path) == ("POST", _CREATE_BACKUP_PATH) else 30
        try:
            async with self.session.request(
                method, "http://supervisor" + path, json=body,
                timeout=aiohttp.ClientTimeout(total=request_timeout), allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise RepairBlocked("supervisor_request_rejected")
                data = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise RepairBlocked("supervisor_request_unavailable") from exc
        if not isinstance(data, dict) or data.get("result") != "ok":
            raise RepairBlocked("supervisor_result_not_ok")
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise RepairBlocked("supervisor_result_invalid")
        return payload

    async def jobs(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/jobs/info")
        if not isinstance(data.get("jobs"), list):
            raise RepairBlocked("job_evidence_unavailable")
        return _all_jobs(data["jobs"])

    async def preflight(self, required_free_bytes: int) -> None:
        host = await self._request("GET", "/host/info")
        free = host.get("disk_free")
        if isinstance(free, bool) or not isinstance(free, (int, float)):
            raise RepairBlocked("host_free_space_unavailable")
        if not math.isfinite(free) or free * 1_000_000_000 < required_free_bytes:
            raise RepairBlocked("insufficient_backup_space")
        jobs = await self.jobs()
        if any("backup" in str(job.get("name", "")).lower()
               and job.get("done") is not True for job in jobs):
            raise RepairBlocked("another_backup_job_is_active")

    async def create(
        self, *, name: str, password: str, marker: dict[str, str],
    ) -> tuple[str, str]:
        try:
            data = await self._request(
                "POST", _CREATE_BACKUP_PATH,
                body={
                    "name": name, "password": password, "compressed": True,
                    "homeassistant": True, "homeassistant_exclude_database": True,
                    "addons": [], "folders": [], "location": _LOCAL_BACKUP_LOCATION,
                    "background": False, "extra": {"autodoctor": marker},
                },
            )
            slug = valid_id(data.get("slug"))
            job_id = valid_id(data.get("job_id"))
        except RepairBlocked as exc:
            # Even a rejected/malformed response may hide a completed backup.
            raise BackupUncertain("backup_creation_outcome_uncertain") from exc
        return slug, job_id

    async def verify_job(self, job_id: str) -> None:
        matches = [job for job in await self.jobs() if job.get("uuid") == valid_id(job_id)]
        if len(matches) != 1 or not job_succeeded(matches[0]):
            raise BackupUncertain("backup_job_not_confirmed_complete")

    async def inspect(self, slug: str) -> dict[str, Any]:
        return await self._request("GET", f"/backups/{valid_id(slug)}/info")

    async def delete_local(self, slug: str) -> None:
        await self._request(
            "DELETE", f"/backups/{valid_id(slug)}", body={"location": [_LOCAL_BACKUP_LOCATION]},
        )

    @staticmethod
    def validate_snapshot(
        info: dict[str, Any], *, slug: str, marker: dict[str, str], max_bytes: int,
    ) -> int:
        expected = (
            info.get("slug") == slug,
            info.get("type") == "partial",
            info.get("protected") is True,
            isinstance(info.get("homeassistant"), str) and bool(info["homeassistant"]),
            info.get("homeassistant_exclude_database") is True,
            info.get("addons") == [],
            info.get("folders") == [],
            info.get("location") in (None, _LOCAL_BACKUP_LOCATION),
            (info.get("extra") or {}).get("autodoctor") == marker,
        )
        if not all(expected):
            raise RepairBlocked("backup_ownership_or_content_mismatch")
        size = positive_size(info.get("size_bytes"))
        if size > max_bytes:
            raise RepairBlocked("backup_exceeds_size_limit")
        return size
