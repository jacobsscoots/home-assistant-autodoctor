from __future__ import annotations

import sqlite3

from .store import IncidentStore


class SeedAwareIncidentStore(IncidentStore):
    """Incident store that only installs bundled seed knowledge when explicitly enabled."""

    def __init__(self, path: str, *, seed_enabled: bool = False) -> None:
        super().__init__(path)
        self.seed_enabled = bool(seed_enabled)

    def _seed_knowledge(self, db: sqlite3.Connection) -> None:
        if self.seed_enabled:
            super()._seed_knowledge(db)
