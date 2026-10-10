from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json

import pytest

from test_backup_first_repairs import stack
from autodoctor.github_mirror import GitHubHistoryMirror, GitHubRequestRejected, structured_history
from autodoctor.models import LogEvent
from autodoctor.integration_planner import IntegrationReloadPlanner
from autodoctor.proactive import ProactiveMonitor
from test_backup_first_repairs import verify


class FakeGitHub:
    def __init__(self):
        self.issues = []
        self.calls = []
        self.lose_create = False
        self.apply_lost_create = False
        self.reject_create = False
        self.offline = False

    async def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if self.offline:
            raise TimeoutError("private transport error")
        if method == "GET" and "?" in path:
            return [dict(x) for x in self.issues]
        if method == "POST":
            if self.reject_create:
                raise GitHubRequestRejected(403)
            item = {"number": len(self.issues) + 1, **payload, "state": "open"}
            if not self.lose_create or self.apply_lost_create:
                self.issues.append(item)
            if self.lose_create:
                raise TimeoutError("response lost")
            return dict(item)
        number = int(path.rsplit("/", 1)[1])
        item = next(x for x in self.issues if x["number"] == number)
        if method == "PATCH":
            item.update(payload)
        return dict(item)


async def record_case(store, cases, clock, *, pattern="private/entity/identity"):
    cases._now = lambda: clock[0]
    fp = hashlib.sha256(pattern.encode()).hexdigest()[:20]
    event = LogEvent("ERROR", "private source", "", "Secret token=never-export sensor.private_name failed at 192.168.1.2",
                     "private.family", clock[0])
    _, new = await store.record(fp, event, pattern, "timeout")
    await cases.record_event(pattern_key=pattern, pattern_label="timeout", family="private.family", fingerprint=fp,
                             event=event, fingerprint_is_new=new)
    return pattern


async def mirror_for(settings, store, clock, remote):
    settings = replace(settings, github_history_enabled=True, github_history_repository="example/incident-history",
                       github_history_token="test-only-token")
    mirror = GitHubHistoryMirror(settings, store.path, clock=lambda: clock[0])
    await mirror.initialize()
    mirror._request = remote.request
    return mirror


def test_durable_mirror_deduplicates_recurrence_and_exports_only_structured_data(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            pattern = await record_case(store, cases, clock)
            remote = FakeGitHub()
            mirror = await mirror_for(s, store, clock, remote)
            await mirror.run_once()
            await mirror.run_once()
            assert len(remote.issues) == 1
            assert len([x for x in remote.calls if x[0] == "POST"]) == 1
            serialized = json.dumps(remote.calls)
            for private in ("never-export", "sensor.private_name", "192.168.1.2", "private.family", pattern, "test-only-token"):
                assert private not in serialized
            clock[0] += 60
            await record_case(store, cases, clock)
            restarted = await mirror_for(s, store, clock, remote)
            await restarted.run_once()
            assert len(remote.issues) == 1
            assert '"occurrences": 2' in remote.issues[0]["body"]
            assert remote.issues[0]["state"] == "open"
    asyncio.run(run())


@pytest.mark.parametrize("applied", [True, False])
def test_lost_issue_create_response_is_never_blindly_replayed_across_restart(tmp_path, applied):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            await record_case(store, cases, clock)
            remote = FakeGitHub()
            remote.lose_create, remote.apply_lost_create = True, applied
            mirror = await mirror_for(s, store, clock, remote)
            await mirror.run_once()
            assert mirror.health()["uncertain_creates"] == 1
            clock[0] += 61
            restarted = await mirror_for(s, store, clock, remote)
            remote.lose_create = False
            await restarted.run_once()
            assert len([x for x in remote.calls if x[0] == "POST"]) == 1
            assert len(remote.issues) == int(applied)
            assert restarted.health()["uncertain_creates"] == int(not applied)
    asyncio.run(run())


def test_definitive_rejected_create_can_retry_after_credentials_are_fixed(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            await record_case(store, cases, clock)
            remote = FakeGitHub()
            remote.reject_create = True
            mirror = await mirror_for(s, store, clock, remote)
            await mirror.run_once()
            assert mirror.health()["uncertain_creates"] == 0
            remote.reject_create = False
            clock[0] += 61
            await mirror.run_once()
            assert len(remote.issues) == 1
    asyncio.run(run())


def test_github_outage_does_not_interrupt_local_incident_storage(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            await record_case(store, cases, clock)
            remote = FakeGitHub()
            remote.offline = True
            mirror = await mirror_for(s, store, clock, remote)
            await mirror.run_once()
            assert mirror.health()["pending_updates"] == 1
            clock[0] += 61
            await record_case(store, cases, clock)
            remote.offline = False
            await mirror.run_once()
            assert '"occurrences": 2' in remote.issues[0]["body"]
            assert (await cases.list_cases())[0]["occurrences"] == 2
    asyncio.run(run())


def test_ambiguous_marker_never_creates_or_overwrites_an_issue(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            pattern = await record_case(store, cases, clock)
            remote = FakeGitHub()
            marker = "<!-- autodoctor-case:" + hashlib.sha256(pattern.encode()).hexdigest() + " -->"
            remote.issues = [{"number": 1, "body": marker}, {"number": 2, "body": marker}]
            mirror = await mirror_for(s, store, clock, remote)
            await mirror.run_once()
            assert all(x[0] == "GET" for x in remote.calls)
    asyncio.run(run())


def test_changed_repository_never_reuses_the_old_issue_mapping(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            await record_case(store, cases, clock)
            mirror = await mirror_for(s, store, clock, FakeGitHub())
            await mirror.run_once()
            remote = FakeGitHub()
            settings = replace(mirror.settings, github_history_repository="example/different-history")
            second = GitHubHistoryMirror(settings, store.path, clock=lambda: clock[0])
            await second.initialize()
            second._request = remote.request
            await second.run_once()
            assert len([x for x in remote.calls if x[0] == "POST"]) == 1
            assert all("/repos/example/different-history/" in x[1] for x in remote.calls)
    asyncio.run(run())


def test_manual_resolution_never_claims_a_verified_repair_or_closes_the_issue(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            pattern = await record_case(store, cases, clock)
            remote = FakeGitHub()
            mirror = await mirror_for(s, store, clock, remote)
            await mirror.run_once()
            clock[0] += 60
            await cases.mark_resolved(pattern)
            await mirror.run_once()
            assert remote.issues[0]["state"] == "open"
            assert '"verified_autodoctor_repair": false' in remote.issues[0]["body"]
    asyncio.run(run())


def test_structured_export_refuses_unknown_labels_and_untrusted_fingerprints():
    case = {"pattern_key": "example", "pattern_label": "token=private", "status": "new",
            "representative_fingerprint": "a" * 20, "first_seen": 1, "last_seen": 2, "occurrences": 1}
    assert structured_history(case, False) is None
    case["pattern_label"], case["representative_fingerprint"] = "timeout", "token=private"
    assert structured_history(case, False) is None


def test_disabled_or_unconfigured_mirror_makes_no_network_requests(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            await record_case(store, cases, clock)
            remote = FakeGitHub()
            for enabled in (False, True):
                mirror = GitHubHistoryMirror(replace(s, github_history_enabled=enabled), store.path)
                await mirror.initialize()
                mirror._request = remote.request
                await mirror.run_once()
            assert remote.calls == []
    asyncio.run(run())


def test_verified_backed_up_repair_closes_history_and_recurrence_reopens_it(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, ex, ha, _, _, clock):
            cases._now = lambda: clock[0]
            planner = IntegrationReloadPlanner(s, cases, ha)
            probe = ProactiveMonitor(s, store, cases, ha, planner, clock=lambda: clock[0])
            await probe.initialize()
            await probe.run_once()
            clock[0] += 180
            await probe.run_once()
            plan = (await cases.list_repair_plans())[0]
            remote = FakeGitHub()
            mirror = await mirror_for(s, store, clock, remote)
            await mirror.run_once()
            assert remote.issues[0]["state"] == "open"
            result = await ex.auto_execute(plan["plan_id"])
            assert (await verify(ex, result, clock))["stage"] == "succeeded"
            await mirror.run_once()
            assert remote.issues[0]["state"] == "closed"
            assert '"verified_autodoctor_repair": true' in remote.issues[0]["body"]
            ha.state = "setup_retry"
            clock[0] += 60
            await probe.run_once()
            await mirror.run_once()
            assert remote.issues[0]["state"] == "open"
            assert '"verified_autodoctor_repair": false' in remote.issues[0]["body"]
            assert len(remote.issues) == 1
    asyncio.run(run())


def test_backdated_evidence_still_updates_history_without_restamping_first_or_last_seen(tmp_path):
    async def run():
        async with stack(tmp_path) as (s, store, cases, _, _, _, _, clock):
            pattern = await record_case(store, cases, clock)
            first = (await cases.get_case(pattern))["first_seen"]
            remote = FakeGitHub()
            mirror = await mirror_for(s, store, clock, remote)
            await mirror.run_once()
            clock[0] += 60
            event = LogEvent("ERROR", "test.py", "", "old observation", "test", first - 10)
            await cases.record_event(pattern_key=pattern, pattern_label="timeout", family="test",
                                     fingerprint=hashlib.sha256(pattern.encode()).hexdigest()[:20],
                                     event=event, fingerprint_is_new=False)
            await mirror.run_once()
            assert '"occurrences": 2' in remote.issues[0]["body"]
            case = await cases.get_case(pattern)
            assert case["first_seen"] == first
            assert case["last_seen"] == event.timestamp
            assert case["updated_at"] == clock[0]
    asyncio.run(run())
