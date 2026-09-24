from __future__ import annotations

import asyncio
import copy
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.diagnostic_recipe import DiagnosticHAClient
from autodoctor.repair_backup import BackupUncertain, SupervisorBackupClient


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_backup_post_is_fixed_encrypted_local_and_never_retried():
    async def run():
        session = Session([Response({"result": "ok", "data": {"slug": "backup_test", "job_id": "job_test"}})])
        client = SupervisorBackupClient(session)
        password = secrets.token_urlsafe(24)
        result = await client.create(name="Test", password=password, marker={"owner": "test"})
        assert result == ("backup_test", "job_test")
        assert len(session.calls) == 1
        args, options = session.calls[0]
        assert args == ("POST", "http://supervisor/backups/new/partial")
        assert options["allow_redirects"] is False
        assert options["json"]["password"] == password
        assert options["json"]["homeassistant"] is True
        assert options["json"]["homeassistant_exclude_database"] is True
        assert options["json"]["addons"] == []
        assert options["json"]["folders"] == []
        assert options["json"]["location"] == ".local"
        assert options["json"]["background"] is False
        assert options["timeout"].total == 600
    asyncio.run(run())


@pytest.mark.parametrize("result", [TimeoutError(), Response({"result": "ok", "data": {"job_id": "job_only"}})])
def test_uncertain_backup_response_never_replays_post(result):
    async def run():
        session = Session([result])
        client = SupervisorBackupClient(session)
        password = secrets.token_urlsafe(24)
        with pytest.raises(BackupUncertain, match="outcome_uncertain"):
            await client.create(name="Test", password=password, marker={})
        assert len(session.calls) == 1
    asyncio.run(run())


def test_delete_is_scoped_to_recorded_local_slug():
    async def run():
        session = Session([Response({"result": "ok", "data": {}})])
        await SupervisorBackupClient(session).delete_local("backup_test")
        args, options = session.calls[0]
        assert args == ("DELETE", "http://supervisor/backups/backup_test")
        assert options["json"] == {"location": [".local"]}
        assert options["timeout"].total == 30
        assert len(session.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("changed", [False, True])
def test_diagnostic_verification_requires_run_of_exact_postimage(changed):
    async def run():
        now = datetime.now(timezone.utc)
        expected = {"id": "test_diagnostic", "actions": [{"action": "system_log.write", "data": {"message": "test"}}]}
        trace = {"domain": "automation", "item_id": "test_diagnostic", "run_id": "run_test",
                 "state": "stopped", "script_execution": "finished", "timestamp": {"start": now.isoformat()}}
        detail = {**trace, "config": copy.deepcopy(expected)}
        if changed:
            detail["config"]["actions"][0]["data"]["message"] = "old configuration"
        reads = []

        async def summaries(config_id):
            return [trace]

        async def details(config_id, run_id):
            reads.append((config_id, run_id))
            return detail

        ha = SimpleNamespace(read_automation_traces=summaries, read_automation_trace=details)
        verified = await DiagnosticHAClient(ha).natural_run_verified("test_diagnostic", now.timestamp() - 1, expected)
        assert verified is (not changed)
        assert reads == [("test_diagnostic", "run_test")]
    asyncio.run(run())
