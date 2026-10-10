from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from autodoctor.audit_access import ingress_or_authenticated_qualification
from autodoctor.ha import HomeAssistantClient
from autodoctor.models import LogEvent
from autodoctor.repair_dashboard import RepairDashboard
from autodoctor.runtime_health import WorkerSupervisor


def test_worker_restart_is_bounded_and_does_not_log_exception_details(monkeypatch, caplog):
    async def run():
        delays = []
        original_sleep = asyncio.sleep

        async def sleep(delay):
            delays.append(delay)
            await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", sleep)
        supervisor = WorkerSupervisor()
        calls = 0

        async def fail():
            nonlocal calls
            calls += 1
            raise RuntimeError("sensitive credential example")

        task = supervisor.start("test-worker", fail)
        assert supervisor.start("test-worker", fail) is task
        await task
        assert calls == 5
        assert delays == [2, 4, 8, 16]
        assert supervisor.snapshot()["alive"] is False
        assert "sensitive credential example" not in caplog.text
        await supervisor.close()
    asyncio.run(run())


def test_quiet_watcher_is_healthy_but_expired_worker_heartbeat_is_not():
    async def run():
        clock = [0.0]
        supervisor = WorkerSupervisor(clock=lambda: clock[0])

        async def idle():
            await asyncio.Event().wait()

        supervisor.start("watcher", idle)
        supervisor.start("scan", idle, max_silence=60)
        await asyncio.sleep(0)
        clock[0] = 61
        snapshot = supervisor.snapshot()
        assert snapshot["workers"]["watcher"]["state"] == "running"
        assert snapshot["workers"]["scan"]["state"] == "stalled"
        supervisor.beat("scan")
        assert supervisor.snapshot()["alive"] is True
        await supervisor.close()
    asyncio.run(run())


@pytest.mark.parametrize("remote,path,method,allowed", [
    ("172.30.32.1", "/live", "GET", True),
    ("192.168.1.10", "/live", "GET", False),
    ("172.30.33.10", "/api/health", "GET", False),
    ("172.30.33.10", "/live", "POST", False),
])
def test_watchdog_does_not_open_the_diagnostic_or_write_routes(remote, path, method, allowed):
    async def run():
        request = SimpleNamespace(remote=remote, path=path, method=method, headers={})
        async def handler(_request):
            return web.Response(text="probe")
        if allowed:
            response = await ingress_or_authenticated_qualification(request, handler)
            assert response.status == 200
        else:
            with pytest.raises(web.HTTPForbidden):
                await ingress_or_authenticated_qualification(request, handler)
    asyncio.run(run())


def test_liveness_is_minimal_and_does_not_depend_on_ai_or_ha_availability():
    async def run():
        supervisor = WorkerSupervisor()
        async def idle():
            await asyncio.Event().wait()
        supervisor.start("watcher", idle)
        dashboard = RepairDashboard.__new__(RepairDashboard)
        dashboard.engine = SimpleNamespace(runtime=supervisor)
        response = await dashboard.liveness(None)
        assert response.status == 200
        assert json.loads(response.text) == {"alive": True}
        await supervisor.close()
        assert (await dashboard.liveness(None)).status == 503
    asyncio.run(run())


def test_clean_ha_stream_close_reconnects_with_delay(monkeypatch):
    async def run():
        ha = HomeAssistantClient.__new__(HomeAssistantClient)
        ha.ws_url = "ws://test.invalid"
        ha.watcher_connected = False
        ha.watcher_last_connected_at = ha.watcher_last_event_at = None
        ha.watcher_reconnects = 0
        connections, delays = [], []
        class Session:
            def ws_connect(self, *args, **kwargs):
                class Context:
                    async def __aenter__(self):
                        connections.append(1)
                        return len(connections)
                    async def __aexit__(self, *args):
                        pass
                return Context()
        ha.session = Session()
        async def subscribe(_ws):
            pass
        async def events(ws):
            if ws == 2:
                yield LogEvent("ERROR", "test.py", "", "test observation", "test", 1)
        async def sleep(delay):
            delays.append(delay)
        ha._subscribe_system_log, ha._iter_system_log_events = subscribe, events
        monkeypatch.setattr(asyncio, "sleep", sleep)
        stream = ha.system_log_events()
        assert (await anext(stream)).message == "test observation"
        assert len(connections) == 2
        assert delays == [2]
        assert ha.watcher_health()["connected"] is True
        await stream.aclose()
        assert ha.watcher_health()["connected"] is False
    asyncio.run(run())
