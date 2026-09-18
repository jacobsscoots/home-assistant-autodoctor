"""Short-lived, coordinated SQLite access for AutoDoctor's worker threads."""
from __future__ import annotations

import sqlite3
from _thread import RLock
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from weakref import WeakValueDictionary

_DATABASE_LOCKS: WeakValueDictionary[str, RLock] = WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()


def _database_lock(path: str) -> RLock:
    # Canonicalise aliases so readers, writers and read-only audits share one gate.
    key = str(Path(path).resolve())
    with _REGISTRY_LOCK:
        lock = _DATABASE_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _DATABASE_LOCKS[key] = lock
        return lock


@contextmanager
def database_connection(
    path: str, *, readonly: bool = False, timeout: float = 5.0,
) -> Iterator[sqlite3.Connection]:
    """Commit/rollback and close before allowing another local DB operation.

    Call only from synchronous DB workers (normally via asyncio.to_thread), never
    across an await or a Home Assistant/AI request. A worker owns the gate until it
    actually exits, even when the coroutine waiting for that worker is cancelled.
    Both local gate acquisition and SQLite's external-lock wait are bounded. No
    SQL operation, transaction body or external repair action is replayed.
    """
    lock = _database_lock(path)
    if not lock.acquire(timeout=timeout):
        raise sqlite3.OperationalError("database is locked (local access timeout)")
    try:
        target = Path(path).resolve().as_uri() + "?mode=ro" if readonly else path
        db = sqlite3.connect(target, timeout=timeout, uri=readonly)
        try:
            with db:
                yield db
        finally:
            # sqlite3.Connection.__exit__ handles transactions, not close().
            db.close()
    finally:
        lock.release()
