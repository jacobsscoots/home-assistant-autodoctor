from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.automatic_repair import AutoApplyRepairExecutor, AutomaticRepairCoordinator
from autodoctor.cases import IncidentCaseManager
from autodoctor.config import Settings
from autodoctor.models import LogEvent
from autodoctor.store import IncidentStore


def test_store_connections_are_closed_on_return(tmp_path: Path, monkeypatch) -> None:
    class TrackedConnection(sqlite3.Connection):
        was_closed = False

        def close(self) -> None:
            super().close()
            self.was_closed = True

    connections: list[TrackedConnection] = []
    original_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        db = original_connect(*args, factory=TrackedConnection, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)

    async def run() -> None:
        path = str(tmp_path / "closed.db")
        store = IncidentStore(path)
        await store.initialize()
        cases = IncidentCaseManager(path, None, notifications_enabled=False)
        await cases.initialize()
        executor = AutoApplyRepairExecutor(Settings(), path, None, None, cases)
        await executor.initialize()
        await executor.health()
        await cases.list_repair_plans()
        await store.list_recent(5)

    asyncio.run(run())
    assert connections
    assert all(db.was_closed for db in connections)


def test_coordinator_waits_for_incident_writer_without_lock_error(tmp_path: Path, monkeypatch) -> None:
    writer_entered = threading.Event()
    release_writer = threading.Event()
    reader_entered = threading.Event()
    original_connect = sqlite3.connect

    class ExclusiveIncidentConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.lstrip().startswith("INSERT INTO incidents") and not self.in_transaction:
                super().execute("BEGIN EXCLUSIVE")
            return super().execute(sql, parameters)

    def short_timeout_connect(*args, **kwargs):
        kwargs.update(timeout=0.02, factory=ExclusiveIncidentConnection)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", short_timeout_connect)

    async def run() -> None:
        path = str(tmp_path / "contention.db")
        store = IncidentStore(path)
        await store.initialize()
        cases = IncidentCaseManager(path, None, notifications_enabled=False)
        await cases.initialize()
        settings = Settings(auto_apply_low_risk=True, repair_executor_enabled=True)
        executor = AutoApplyRepairExecutor(settings, path, None, None, cases)
        await executor.initialize()
        coordinator = AutomaticRepairCoordinator(settings, cases, executor)
        original_prune = store._prune_incidents_sync
        original_poll = executor._pending_verification_ids_sync

        def hold_writer(db, **kwargs):
            original_prune(db, **kwargs)
            writer_entered.set()
            assert release_writer.wait(2), "test writer was not released"

        def signal_reader():
            reader_entered.set()
            return original_poll()

        monkeypatch.setattr(store, "_prune_incidents_sync", hold_writer)
        monkeypatch.setattr(executor, "_pending_verification_ids_sync", signal_reader)
        event = LogEvent("ERROR", "test.py", "", "test incident", "test", 1000)
        writer = asyncio.create_task(store.record("fp", event))
        reader = None
        try:
            assert await asyncio.to_thread(writer_entered.wait, 1)
            reader = asyncio.create_task(coordinator.run_once())
            assert await asyncio.to_thread(reader_entered.wait, 1)
            # The event loop remains responsive while the DB worker waits. Hold the
            # writer longer than SQLite's test timeout to reproduce the old failure.
            await asyncio.sleep(0.08)
        finally:
            release_writer.set()
            await writer
        assert reader is not None
        assert await reader == 0
        assert (await store.list_recent(1))[0]["occurrences"] == 1
        assert await cases.list_repair_plans() == []
        await executor.close()

    asyncio.run(run())


def test_managed_connection_rolls_back_and_closes_on_failure(tmp_path: Path) -> None:
    from autodoctor.database import database_connection

    path = str(tmp_path / "rollback.db")
    with database_connection(path) as db:
        db.execute("CREATE TABLE sample (value INTEGER)")
    with pytest.raises(ValueError, match="abort"):
        with database_connection(path) as failed_db:
            failed_db.execute("INSERT INTO sample VALUES (1)")
            raise ValueError("abort")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed_db.execute("SELECT 1")
    with database_connection(path, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0


def test_readonly_access_escapes_uri_characters_and_never_creates_database(tmp_path: Path) -> None:
    from autodoctor.database import database_connection

    path = str(tmp_path / "audit ?# data.db")
    with database_connection(path) as db:
        db.execute("CREATE TABLE sample (value INTEGER)")
    with database_connection(path, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("INSERT INTO sample VALUES (1)")
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        with database_connection(str(missing), readonly=True):
            pytest.fail("read-only access must not create a database")
    assert not missing.exists()


def test_external_lock_failure_is_bounded_and_does_not_replay_body(tmp_path: Path) -> None:
    from contextlib import closing
    from time import monotonic
    from autodoctor.database import database_connection

    path = str(tmp_path / "external.db")
    with database_connection(path) as db:
        db.execute("CREATE TABLE sample (value INTEGER)")
    entered = []
    with closing(sqlite3.connect(path)) as external:
        external.execute("BEGIN EXCLUSIVE")
        started = monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with database_connection(path, timeout=0.02) as db:
                entered.append(True)
                db.execute("INSERT INTO sample VALUES (1)")
        assert monotonic() - started < 1
        external.rollback()
    assert entered == [True]
    with database_connection(path) as db:
        assert db.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0


def test_same_database_path_aliases_share_a_bounded_gate(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from autodoctor.database import database_connection

    path = str(tmp_path / "same.db")
    alias = str(tmp_path) + "/./same.db"
    with database_connection(path):
        with ThreadPoolExecutor(max_workers=1) as pool:
            def access_alias() -> None:
                with database_connection(alias, timeout=0.02):
                    pytest.fail("the first connection still owns the gate")
            with pytest.raises(sqlite3.OperationalError, match="local access timeout"):
                pool.submit(access_alias).result(timeout=1)
    with database_connection(alias, timeout=0.02) as db:
        assert db.execute("SELECT 1").fetchone() == (1,)


def test_cancelling_coroutine_does_not_release_its_worker_database_gate(tmp_path: Path) -> None:
    from autodoctor.database import database_connection

    entered, release = threading.Event(), threading.Event()
    path = str(tmp_path / "cancelled.db")

    def writer() -> None:
        with database_connection(path) as db:
            db.execute("CREATE TABLE sample (value INTEGER)")
            db.execute("INSERT INTO sample VALUES (1)")
            entered.set()
            assert release.wait(2)

    def reader() -> int:
        with database_connection(path) as db:
            return db.execute("SELECT COUNT(*) FROM sample").fetchone()[0]

    async def run() -> None:
        task = asyncio.create_task(asyncio.to_thread(writer))
        pending = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pending = asyncio.create_task(asyncio.to_thread(reader))
            await asyncio.sleep(0.05)
            assert not pending.done()
        finally:
            release.set()
        assert pending is not None and await pending == 1

    asyncio.run(run())


def test_multiple_store_instances_preserve_every_incident_increment(tmp_path: Path) -> None:
    async def run() -> None:
        stores = [IncidentStore(str(tmp_path / "shared.db")) for _ in range(3)]
        for store in stores:
            await store.initialize()
        event = LogEvent("ERROR", "test.py", "", "Test", "test", 1000)
        results = await asyncio.gather(*(stores[n % 3].record("same-fp", event) for n in range(60)))
        assert sum(is_new for _, is_new in results) == 1
        assert (await stores[0].list_recent(1))[0]["occurrences"] == 60

    asyncio.run(run())


def test_runtime_database_users_do_not_open_unmanaged_connections() -> None:
    modules = (
        "store", "cases", "repair_executor", "automatic_repair", "case_lifecycle",
        "case_consistency", "ai_usage_recovery", "qualification",
    )
    for module in modules:
        source = (ROOT / "autodoctor" / f"{module}.py").read_text()
        assert "sqlite3.connect(" not in source, module
        assert "database_connection(" in source, module
