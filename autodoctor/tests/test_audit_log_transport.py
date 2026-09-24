from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from test_audit_log_recipe import ENTITY, SCRIPT_KEY, logger_config
from test_audit_log_execution import settings_options, trace_fixture
from autodoctor.audit_log_client import AuditLogHAClient
from autodoctor.audit_log_recipe import compile_repair
from autodoctor.repair_backup import RepairBlocked


class Response:
    status = 200
    def __init__(self, data):
        self.data = data
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    async def json(self):
        return copy.deepcopy(self.data)


class NativeHA:
    api_base = "http://supervisor/core/api"
    def __init__(self):
        self.session = self
        self.version = "2026.9.3"
        self.stored = logger_config()
        self.loaded = copy.deepcopy(self.stored)
        self.registry = {"entity_id": ENTITY, "platform": "script", "unique_id": SCRIPT_KEY}
        self.state = {"state": "off", "attributes": {"current": 0}}
        self.posts = []
        self.reads = []
        self.trace, _ = trace_fixture()
    async def get_version(self):
        return self.version
    async def get_state(self, entity):
        assert entity == ENTITY
        return self.state
    async def _repair_read(self, payload):
        self.reads.append(payload)
        kind = payload["type"]
        if kind == "config/entity_registry/get":
            return self.registry
        if kind == "script/config":
            return {"config": self.loaded}
        if kind == "trace/list":
            return [self.trace]
        if kind == "trace/get":
            return self.trace
        raise AssertionError("Unexpected native command")
    def get(self, url, **kwargs):
        assert url == self.api_base + "/config/script/config/" + SCRIPT_KEY
        assert kwargs["allow_redirects"] is False
        return Response(self.stored)
    def post(self, url, *, json, **kwargs):
        assert url == self.api_base + "/config/script/config/" + SCRIPT_KEY
        assert kwargs["allow_redirects"] is False
        self.posts.append(copy.deepcopy(json))
        self.stored = copy.deepcopy(json)
        return Response({"result": "ok"})


def client_pair():
    ha = NativeHA()
    return ha, AuditLogHAClient(ha, SimpleNamespace(**settings_options()))


def test_identity_loaded_source_and_idle_checks_before_exact_native_save():
    async def run():
        ha, client = client_pair()
        key, current = await client.resolve()
        await client.write_checked(key, current, compile_repair(current))
        assert ha.posts == [compile_repair(logger_config())]
        assert {read["type"] for read in ha.reads} == {"config/entity_registry/get", "script/config"}
    asyncio.run(run())


@pytest.mark.parametrize("mutate", [
    lambda h: setattr(h, "version", "2026.9.4"),
    lambda h: h.state.update(state="on"),
    lambda h: h.state["attributes"].update(current=1),
    lambda h: h.state["attributes"].update(current=False),
    lambda h: h.state["attributes"].pop("current"),
    lambda h: h.registry.update(platform="other"),
    lambda h: h.registry.update(entity_id="script.unrelated"),
    lambda h: h.registry.update(unique_id="../escape"),
    lambda h: h.loaded.update(description="Changed but not saved"),
])
def test_native_preconditions_refuse_ambiguous_busy_or_unreviewed_state(mutate):
    async def run():
        ha, client = client_pair()
        mutate(ha)
        with pytest.raises(RepairBlocked):
            await client.resolve()
        assert ha.posts == []
    asyncio.run(run())


@pytest.mark.parametrize("mutate", [
    lambda h: h.stored.update(description="Intervening saved change"),
    lambda h: h.loaded.update(description="Intervening loaded change"),
    lambda h: h.state.update(state="on"),
    lambda h: h.registry.update(unique_id="other_target"),
    lambda h: setattr(h, "version", "2026.9.4"),
])
def test_last_moment_edit_or_start_blocks_save(mutate):
    async def run():
        ha, client = client_pair()
        key, before = await client.resolve()
        mutate(ha)
        after = compile_repair(before)
        with pytest.raises(RepairBlocked):
            await client.write_checked(key, before, after)
        assert ha.posts == []
    asyncio.run(run())


def test_native_verification_reads_script_traces_without_triggering():
    async def run():
        ha, client = client_pair()
        assert await client.natural_run_verified(SCRIPT_KEY, 0, compile_repair(logger_config()))
        assert [read["type"] for read in ha.reads] == ["trace/list", "trace/get"]
        assert all(read["domain"] == "script" for read in ha.reads)
        assert ha.posts == []
    asyncio.run(run())
