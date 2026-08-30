from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.seed_store import SeedAwareIncidentStore


def knowledge_count(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0])


def test_fresh_database_has_no_bundled_seeds_by_default(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    store = SeedAwareIncidentStore(str(path))
    asyncio.run(store.initialize())
    assert knowledge_count(path) == 0


def test_bundled_seeds_require_explicit_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "seeded.db"
    store = SeedAwareIncidentStore(str(path), seed_enabled=True)
    asyncio.run(store.initialize())
    assert knowledge_count(path) > 0


def test_disabling_seeds_does_not_delete_existing_memory(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    asyncio.run(SeedAwareIncidentStore(str(path), seed_enabled=True).initialize())
    before = knowledge_count(path)

    asyncio.run(SeedAwareIncidentStore(str(path), seed_enabled=False).initialize())

    assert knowledge_count(path) == before
