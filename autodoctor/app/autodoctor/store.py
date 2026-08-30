from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .budget import month_bounds_utc
from .memory import (
    SEED_KNOWLEDGE,
    analysis_to_memory_text,
    bounded_memory,
    effective_score,
    expiry_timestamp,
    fts_query,
    pattern_signature,
    seed_payload,
    trust_score,
)
from .models import Analysis, LogEvent
from .scheduler import incident_family

_INCIDENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    fingerprint TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    occurrences INTEGER NOT NULL,
    level TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    exception TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    last_analysis_at REAL,
    analysis_json TEXT,
    pattern_key TEXT NOT NULL DEFAULT '',
    pattern_label TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_incidents_last_seen ON incidents(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_pattern ON incidents(pattern_key, last_seen DESC);
"""

_BASE_INCIDENT_COLUMNS = {
    "fingerprint", "first_seen", "last_seen", "occurrences", "level", "name", "source",
    "message", "exception", "status", "last_analysis_at", "analysis_json",
}
_REQUIRED_INCIDENT_COLUMNS = _BASE_INCIDENT_COLUMNS | {"pattern_key", "pattern_label"}

_AI_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    family TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reserved_input_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reserved_cost_usd REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage(ts);
CREATE INDEX IF NOT EXISTS idx_ai_usage_family_ts ON ai_usage(family, ts);
"""

_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    memory_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL DEFAULT '',
    pattern_key TEXT NOT NULL DEFAULT '',
    pattern_label TEXT NOT NULL DEFAULT '',
    family TEXT NOT NULL DEFAULT 'unknown',
    trust_class TEXT NOT NULL,
    trust_score REAL NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    root_cause TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT '',
    verification TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'unknown',
    ha_version TEXT NOT NULL DEFAULT 'unknown',
    autodoctor_version TEXT NOT NULL DEFAULT 'unknown',
    created_at REAL NOT NULL,
    verified_at REAL,
    last_confirmed_at REAL,
    expires_at REAL,
    superseded_by TEXT,
    recurrence_count INTEGER NOT NULL DEFAULT 0,
    baseline_occurrences INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_knowledge_pattern ON knowledge(pattern_key, trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_family ON knowledge(family, trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_expiry ON knowledge(expires_at);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id TEXT PRIMARY KEY,
    alias TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias);

CREATE TABLE IF NOT EXISTS topology_nodes (
    alias TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    family TEXT NOT NULL DEFAULT 'unknown',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topology_nodes_family ON topology_nodes(family, last_seen DESC);

CREATE TABLE IF NOT EXISTS topology_edges (
    source_alias TEXT NOT NULL,
    target_alias TEXT NOT NULL,
    relation TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    observations INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source_alias, target_alias, relation)
);
CREATE INDEX IF NOT EXISTS idx_topology_edges_source ON topology_edges(source_alias, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_topology_edges_target ON topology_edges(target_alias, last_seen DESC);

CREATE TABLE IF NOT EXISTS outcome_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    memory_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outcome_history_fp ON outcome_history(fingerprint, ts DESC);
"""

_V012_AI_USAGE_COLUMNS = {
    "id", "ts", "fingerprint", "provider", "model", "status", "reserved_input_tokens",
    "reserved_output_tokens", "input_tokens", "output_tokens", "reserved_cost_usd", "cost_usd", "error",
}
_REQUIRED_AI_USAGE_COLUMNS = _V012_AI_USAGE_COLUMNS | {"family"}
_LEGACY_AI_USAGE_COLUMNS = {"ts", "fingerprint"}

_HELPER_DOMAINS = {
    "counter", "input_boolean", "input_button", "input_datetime", "input_number", "input_select",
    "input_text", "timer",
}
_CONTROLLER_DOMAINS = {"automation", "script", "scene"}


class IncidentStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.fts_available = False

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with sqlite3.connect(self.path) as db:
            self._ensure_incident_schema(db)
            self._ensure_ai_usage_schema(db)
            db.executescript(_MEMORY_SCHEMA)
            self._ensure_fts(db)
            self._seed_knowledge(db)
            db.commit()

    def _ensure_incident_schema(self, db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(incidents)").fetchall()}
        if not columns:
            db.executescript(_INCIDENT_SCHEMA)
            return
        if not _BASE_INCIDENT_COLUMNS.issubset(columns):
            raise RuntimeError(
                "Unsupported incidents schema; refusing destructive migration. "
                f"Columns: {sorted(columns)}"
            )
        if "pattern_key" not in columns:
            db.execute("ALTER TABLE incidents ADD COLUMN pattern_key TEXT NOT NULL DEFAULT ''")
        if "pattern_label" not in columns:
            db.execute("ALTER TABLE incidents ADD COLUMN pattern_label TEXT NOT NULL DEFAULT ''")
        db.executescript(_INCIDENT_SCHEMA)
        rows = db.execute(
            """SELECT fingerprint, first_seen, level, name, source, message, exception
            FROM incidents WHERE COALESCE(pattern_key, '') = ''"""
        ).fetchall()
        for fp, ts, level, name, source, message, exception in rows:
            event = LogEvent(
                level=str(level), name=str(name), source=str(source), message=str(message),
                exception=str(exception), timestamp=float(ts),
            )
            family = incident_family(event.name, event.source)
            key, label = pattern_signature(event, family)
            db.execute(
                "UPDATE incidents SET pattern_key = ?, pattern_label = ? WHERE fingerprint = ?",
                (key, label, fp),
            )
        db.commit()

    @staticmethod
    def _backfill_ai_usage_families(db: sqlite3.Connection) -> None:
        rows = db.execute(
            """SELECT u.id, i.name, i.source
            FROM ai_usage AS u
            LEFT JOIN incidents AS i ON i.fingerprint = u.fingerprint
            WHERE COALESCE(u.family, '') = ''"""
        ).fetchall()
        updates: list[tuple[str, int]] = []
        for usage_id, name, source in rows:
            if name is None and source is None:
                continue
            family = incident_family(str(name or ""), str(source or ""))
            if family != "unknown":
                updates.append((family, int(usage_id)))
        if updates:
            db.executemany("UPDATE ai_usage SET family = ? WHERE id = ?", updates)

    @staticmethod
    def _ensure_ai_usage_schema(db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(ai_usage)").fetchall()}
        if not columns:
            db.executescript(_AI_USAGE_SCHEMA)
            return
        if _REQUIRED_AI_USAGE_COLUMNS.issubset(columns):
            db.executescript(_AI_USAGE_SCHEMA)
            IncidentStore._backfill_ai_usage_families(db)
            db.commit()
            return
        if _V012_AI_USAGE_COLUMNS.issubset(columns):
            db.execute("ALTER TABLE ai_usage ADD COLUMN family TEXT NOT NULL DEFAULT ''")
            db.executescript(_AI_USAGE_SCHEMA)
            IncidentStore._backfill_ai_usage_families(db)
            db.commit()
            return
        if columns == _LEGACY_AI_USAGE_COLUMNS:
            db.execute("DROP INDEX IF EXISTS idx_ai_usage_ts")
            db.execute("DROP TABLE IF EXISTS ai_usage_legacy_v011")
            db.execute("ALTER TABLE ai_usage RENAME TO ai_usage_legacy_v011")
            db.executescript(_AI_USAGE_SCHEMA)
            db.execute(
                """INSERT INTO ai_usage
                (ts, fingerprint, provider, model, family, status,
                 reserved_input_tokens, reserved_output_tokens,
                 input_tokens, output_tokens, reserved_cost_usd, cost_usd, error)
                SELECT ts, fingerprint, '', '', '', 'legacy_success',
                       0, 0, NULL, NULL, 0, 0, NULL
                FROM ai_usage_legacy_v011"""
            )
            IncidentStore._backfill_ai_usage_families(db)
            db.execute("DROP TABLE ai_usage_legacy_v011")
            db.commit()
            return
        raise RuntimeError(
            "Unsupported ai_usage schema; refusing destructive migration. "
            f"Columns: {sorted(columns)}"
        )

    def _ensure_fts(self, db: sqlite3.Connection) -> None:
        try:
            db.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    memory_key UNINDEXED, family, pattern_label, root_cause, resolution, verification, outcome
                )"""
            )
            self.fts_available = True
        except sqlite3.OperationalError:
            self.fts_available = False

    def _sync_fts_row(self, db: sqlite3.Connection, record: dict[str, Any]) -> None:
        if not self.fts_available:
            return
        db.execute("DELETE FROM knowledge_fts WHERE memory_key = ?", (record["memory_key"],))
        db.execute(
            """INSERT INTO knowledge_fts
            (memory_key, family, pattern_label, root_cause, resolution, verification, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                record["memory_key"], record.get("family", "unknown"), record.get("pattern_label", ""),
                record.get("root_cause", ""), record.get("resolution", ""),
                record.get("verification", ""), record.get("outcome", "unknown"),
            ),
        )

    def _upsert_knowledge_sync(self, db: sqlite3.Connection, record: dict[str, Any]) -> None:
        fields = (
            "memory_key", "fingerprint", "pattern_key", "pattern_label", "family", "trust_class",
            "trust_score", "source", "root_cause", "resolution", "verification", "outcome",
            "ha_version", "autodoctor_version", "created_at", "verified_at", "last_confirmed_at",
            "expires_at", "superseded_by", "recurrence_count", "baseline_occurrences", "metadata_json",
        )
        values = [record.get(field) for field in fields]
        placeholders = ",".join("?" for _ in fields)
        updates = ",".join(f"{field}=excluded.{field}" for field in fields if field != "memory_key")
        db.execute(
            f"INSERT INTO knowledge ({','.join(fields)}) VALUES ({placeholders}) "
            f"ON CONFLICT(memory_key) DO UPDATE SET {updates}",
            values,
        )
        self._sync_fts_row(db, record)

    def _seed_knowledge(self, db: sqlite3.Connection) -> None:
        for seed in SEED_KNOWLEDGE:
            existing = db.execute("SELECT 1 FROM knowledge WHERE memory_key = ?", (seed["memory_key"],)).fetchone()
            if existing:
                continue
            record = seed_payload(seed)
            self._upsert_knowledge_sync(db, record)

    async def record(
        self,
        fp: str,
        event: LogEvent,
        pattern_key: str = "",
        pattern_label: str = "",
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            return await asyncio.to_thread(self._record_sync, fp, event, pattern_key, pattern_label)

    def _record_sync(
        self,
        fp: str,
        event: LogEvent,
        pattern_key: str,
        pattern_label: str,
    ) -> tuple[dict[str, Any], bool]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            existing = db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (fp,)).fetchone()
            is_new = existing is None
            if not pattern_key:
                family = incident_family(event.name, event.source)
                pattern_key, pattern_label = pattern_signature(event, family)
            if is_new:
                db.execute(
                    """INSERT INTO incidents
                    (fingerprint, first_seen, last_seen, occurrences, level, name, source, message, exception,
                     status, pattern_key, pattern_label)
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                    (
                        fp, event.timestamp, event.timestamp, event.level, event.name, event.source,
                        event.message, event.exception, pattern_key, pattern_label,
                    ),
                )
            else:
                db.execute(
                    """UPDATE incidents
                    SET last_seen = ?, occurrences = occurrences + 1, level = ?, name = ?, source = ?,
                        message = ?, exception = ?, pattern_key = ?, pattern_label = ?,
                        status = CASE WHEN status = 'resolved' THEN 'reopened' ELSE status END
                    WHERE fingerprint = ?""",
                    (
                        event.timestamp, event.level, event.name, event.source, event.message, event.exception,
                        pattern_key, pattern_label, fp,
                    ),
                )
            db.commit()
            row = db.execute("SELECT * FROM incidents WHERE fingerprint = ?", (fp,)).fetchone()
            return dict(row), is_new

    async def save_analysis(self, fp: str, analysis: Analysis) -> None:
        payload = json.dumps(analysis.raw or analysis.__dict__, separators=(",", ":"))
        now = datetime.now(tz=timezone.utc).timestamp()
        async with self._lock:
            await asyncio.to_thread(self._save_analysis_sync, fp, payload, now)

    def _save_analysis_sync(self, fp: str, payload: str, now: float) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE incidents SET analysis_json = ?, last_analysis_at = ? WHERE fingerprint = ?",
                (payload, now, fp),
            )
            db.commit()

    async def save_ai_memory(
        self,
        *,
        fingerprint: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        analysis: Analysis,
        occurrences: int,
        ha_version: str,
        autodoctor_version: str,
        expiry_days: int,
        now_ts: float | None = None,
    ) -> None:
        now = float(now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp())
        resolution, verification = analysis_to_memory_text(analysis)
        record = {
            "memory_key": f"ai:{fingerprint}",
            "fingerprint": fingerprint,
            "pattern_key": pattern_key,
            "pattern_label": pattern_label,
            "family": family,
            "trust_class": "ai_hypothesis",
            "trust_score": trust_score("ai_hypothesis"),
            "source": "autodoctor-diagnosis-only",
            "root_cause": str(analysis.root_cause)[:4000],
            "resolution": resolution,
            "verification": verification,
            "outcome": "unknown",
            "ha_version": ha_version or "unknown",
            "autodoctor_version": autodoctor_version or "unknown",
            "created_at": now,
            "verified_at": None,
            "last_confirmed_at": now,
            "expires_at": expiry_timestamp("ai_hypothesis", now, expiry_days),
            "superseded_by": None,
            "recurrence_count": 0,
            "baseline_occurrences": max(0, int(occurrences)),
            "metadata_json": json.dumps(
                {"confidence": analysis.confidence, "risk": analysis.risk, "action": analysis.action},
                separators=(",", ":"),
            ),
        }
        async with self._lock:
            await asyncio.to_thread(self._save_memory_record_sync, record)

    def _save_memory_record_sync(self, record: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            self._upsert_knowledge_sync(db, record)
            db.commit()

    async def record_recurrence_outcome(
        self,
        fingerprint: str,
        occurrences: int,
        *,
        worsened_recurrences: int,
        now_ts: float | None = None,
    ) -> None:
        now = float(now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp())
        async with self._lock:
            await asyncio.to_thread(
                self._record_recurrence_outcome_sync,
                fingerprint,
                int(occurrences),
                max(2, int(worsened_recurrences)),
                now,
            )

    def _record_recurrence_outcome_sync(
        self,
        fingerprint: str,
        occurrences: int,
        worsened_recurrences: int,
        now: float,
    ) -> None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT * FROM knowledge
                WHERE fingerprint = ? AND trust_class != 'deprecated' AND superseded_by IS NULL""",
                (fingerprint,),
            ).fetchall()
            for row in rows:
                item = dict(row)
                delta = max(0, occurrences - int(item.get("baseline_occurrences") or 0))
                if delta <= 0:
                    continue
                outcome = "worsened" if delta >= worsened_recurrences else "continued"
                previous = str(item.get("outcome") or "unknown")
                db.execute(
                    """UPDATE knowledge
                    SET outcome = ?, recurrence_count = ?, last_confirmed_at = ?
                    WHERE memory_key = ?""",
                    (outcome, delta, now, item["memory_key"]),
                )
                updated = {**item, "outcome": outcome, "recurrence_count": delta, "last_confirmed_at": now}
                self._sync_fts_row(db, updated)
                if previous != outcome:
                    db.execute(
                        """INSERT INTO outcome_history
                        (ts, fingerprint, memory_key, outcome, occurrences, evidence)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            now, fingerprint, item["memory_key"], outcome, occurrences,
                            f"incident recurred {delta} time(s) after this memory was created",
                        ),
                    )
            db.commit()

    async def refresh_quiet_outcomes(self, quiet_seconds: int, now_ts: float | None = None) -> None:
        now = float(now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp())
        cutoff = now - max(3600, int(quiet_seconds))
        async with self._lock:
            await asyncio.to_thread(self._refresh_quiet_outcomes_sync, cutoff, now)

    def _refresh_quiet_outcomes_sync(self, cutoff: float, now: float) -> None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT k.*, i.last_seen
                FROM knowledge AS k
                JOIN incidents AS i ON i.fingerprint = k.fingerprint
                WHERE k.trust_class = 'ai_hypothesis'
                  AND k.outcome = 'unknown'
                  AND i.last_seen <= k.created_at
                  AND i.last_seen < ?""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                item = dict(row)
                db.execute(
                    "UPDATE knowledge SET outcome = 'quiet', last_confirmed_at = ? WHERE memory_key = ?",
                    (now, item["memory_key"]),
                )
                updated = {**item, "outcome": "quiet", "last_confirmed_at": now}
                self._sync_fts_row(db, updated)
                db.execute(
                    """INSERT INTO outcome_history
                    (ts, fingerprint, memory_key, outcome, occurrences, evidence)
                    VALUES (?, ?, ?, 'quiet', ?, ?)""",
                    (
                        now, item["fingerprint"], item["memory_key"],
                        int(item.get("baseline_occurrences") or 0),
                        "no recurrence observed within the configured quiet window; not proof of a fix",
                    ),
                )
            db.commit()

    async def get_or_create_entity_aliases(self, entity_ids: list[str], now_ts: float | None = None) -> dict[str, str]:
        now = float(now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp())
        unique = list(dict.fromkeys(str(x) for x in entity_ids if x))[:50]
        async with self._lock:
            return await asyncio.to_thread(self._get_or_create_entity_aliases_sync, unique, now)

    def _get_or_create_entity_aliases_sync(self, entity_ids: list[str], now: float) -> dict[str, str]:
        aliases: dict[str, str] = {}
        with sqlite3.connect(self.path) as db:
            for entity_id in entity_ids:
                row = db.execute("SELECT alias FROM entity_aliases WHERE entity_id = ?", (entity_id,)).fetchone()
                if row:
                    alias = str(row[0])
                    db.execute("UPDATE entity_aliases SET last_seen = ? WHERE entity_id = ?", (now, entity_id))
                else:
                    domain = self._entity_domain(entity_id)
                    while True:
                        alias = f"{domain}.entity_{secrets.token_hex(4)}"
                        exists = db.execute("SELECT 1 FROM entity_aliases WHERE alias = ?", (alias,)).fetchone()
                        if not exists:
                            break
                    db.execute(
                        """INSERT INTO entity_aliases(entity_id, alias, domain, created_at, last_seen)
                        VALUES (?, ?, ?, ?, ?)""",
                        (entity_id, alias, domain, now, now),
                    )
                aliases[entity_id] = alias
            db.commit()
        return aliases

    async def observe_topology(
        self,
        entity_ids: list[str],
        aliases: dict[str, str],
        family: str,
        now_ts: float,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._observe_topology_sync,
                list(dict.fromkeys(entity_ids))[:30],
                dict(aliases),
                str(family or "unknown"),
                float(now_ts),
            )

    @staticmethod
    def _entity_domain(entity_id: str) -> str:
        domain = entity_id.partition(".")[0]
        return domain or "entity"

    @staticmethod
    def _topology_kind(domain: str) -> str:
        if domain in _CONTROLLER_DOMAINS:
            return "controller"
        if domain in _HELPER_DOMAINS:
            return "helper"
        return "entity"

    @staticmethod
    def _upsert_topology_node(
        db: sqlite3.Connection,
        alias: str,
        kind: str,
        domain: str,
        family: str,
        now: float,
    ) -> None:
        db.execute(
            """INSERT INTO topology_nodes(alias, kind, domain, family, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(alias) DO UPDATE SET
                kind=excluded.kind, domain=excluded.domain, family=excluded.family, last_seen=excluded.last_seen""",
            (alias, kind, domain, family, now, now),
        )

    def _collect_topology_aliases(
        self,
        db: sqlite3.Connection,
        entity_ids: list[str],
        aliases: dict[str, str],
        family: str,
        family_alias: str,
        now: float,
    ) -> tuple[list[str], list[str]]:
        controllers: list[str] = []
        observed_aliases: list[str] = []
        for entity_id in entity_ids:
            alias = aliases.get(entity_id)
            if not alias:
                continue
            domain = self._entity_domain(entity_id)
            kind = self._topology_kind(domain)
            self._upsert_topology_node(db, alias, kind, domain, family, now)
            observed_aliases.append(alias)
            if kind == "controller":
                controllers.append(alias)
            self._upsert_edge(db, alias, family_alias, "observed_family", now)
        return controllers, observed_aliases

    def _link_controller_edges(
        self,
        db: sqlite3.Connection,
        controllers: list[str],
        observed_aliases: list[str],
        now: float,
    ) -> None:
        for controller in controllers:
            for target in observed_aliases:
                if target != controller:
                    self._upsert_edge(db, controller, target, "references_in_incident", now)

    def _link_cooccurrence_edges(
        self,
        db: sqlite3.Connection,
        observed_aliases: list[str],
        now: float,
    ) -> None:
        limited = observed_aliases[:12]
        for index, source in enumerate(limited):
            for target in limited[index + 1 :]:
                self._upsert_edge(db, source, target, "co_occurs_in_incident", now)

    def _link_topology_edges(
        self,
        db: sqlite3.Connection,
        controllers: list[str],
        observed_aliases: list[str],
        now: float,
    ) -> None:
        if controllers:
            self._link_controller_edges(db, controllers, observed_aliases, now)
            return
        self._link_cooccurrence_edges(db, observed_aliases, now)

    def _observe_topology_sync(
        self,
        entity_ids: list[str],
        aliases: dict[str, str],
        family: str,
        now: float,
    ) -> None:
        if not aliases:
            return
        with sqlite3.connect(self.path) as db:
            family_alias = f"family:{family}"
            self._upsert_topology_node(db, family_alias, "family", "", family, now)
            controllers, observed_aliases = self._collect_topology_aliases(
                db, entity_ids, aliases, family, family_alias, now
            )
            self._link_topology_edges(db, controllers, observed_aliases, now)
            db.commit()

    @staticmethod
    def _upsert_edge(db: sqlite3.Connection, source: str, target: str, relation: str, now: float) -> None:
        db.execute(
            """INSERT INTO topology_edges
            (source_alias, target_alias, relation, first_seen, last_seen, observations)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(source_alias, target_alias, relation) DO UPDATE SET
                last_seen=excluded.last_seen, observations=topology_edges.observations+1""",
            (source, target, relation, now, now),
        )

    async def retrieve_memory(
        self,
        *,
        query_text: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        aliases: list[str],
        limit: int,
        max_chars: int,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp())
        async with self._lock:
            return await asyncio.to_thread(
                self._retrieve_memory_sync,
                query_text,
                family,
                pattern_key,
                pattern_label,
                list(dict.fromkeys(aliases))[:20],
                max(1, min(int(limit), 20)),
                max(500, int(max_chars)),
                now,
            )

    def _retrieve_memory_sync(
        self,
        query_text: str,
        family: str,
        pattern_key: str,
        pattern_label: str,
        aliases: list[str],
        limit: int,
        max_chars: int,
        now: float,
    ) -> dict[str, Any]:
        candidates: dict[str, dict[str, Any]] = {}
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT * FROM knowledge
                WHERE trust_class != 'deprecated'
                  AND superseded_by IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND (pattern_key = ? OR family = ?)
                LIMIT 60""",
                (now, pattern_key, family),
            ).fetchall()
            for row in rows:
                candidates[str(row["memory_key"])] = dict(row)

            query = fts_query(query_text, family, pattern_label)
            if self.fts_available and query:
                try:
                    rows = db.execute(
                        """SELECT k.*
                        FROM knowledge_fts
                        JOIN knowledge AS k ON k.memory_key = knowledge_fts.memory_key
                        WHERE knowledge_fts MATCH ?
                          AND k.trust_class != 'deprecated'
                          AND k.superseded_by IS NULL
                          AND (k.expires_at IS NULL OR k.expires_at > ?)
                        LIMIT 60""",
                        (query, now),
                    ).fetchall()
                    for row in rows:
                        candidates[str(row["memory_key"])] = dict(row)
                except sqlite3.OperationalError:
                    pass
            elif query:
                tokens = [part.strip('"') for part in query.split(" OR ")][:6]
                for token in tokens:
                    rows = db.execute(
                        """SELECT * FROM knowledge
                        WHERE trust_class != 'deprecated'
                          AND superseded_by IS NULL
                          AND (expires_at IS NULL OR expires_at > ?)
                          AND (root_cause LIKE ? OR resolution LIKE ? OR verification LIKE ? OR pattern_label LIKE ?)
                        LIMIT 30""",
                        (now, f"%{token}%", f"%{token}%", f"%{token}%", f"%{token}%"),
                    ).fetchall()
                    for row in rows:
                        candidates[str(row["memory_key"])] = dict(row)

            ranked = sorted(
                candidates.values(),
                key=lambda item: effective_score(item, family=family, pattern_key=pattern_key, now=now),
                reverse=True,
            )
            knowledge = bounded_memory(ranked[:limit], max_chars)
            topology = self._topology_slice_sync(db, aliases, family, 30)

        return {
            "knowledge": knowledge,
            "topology": topology,
            "fts_available": self.fts_available,
            "matches": len(knowledge),
        }

    @staticmethod
    def _topology_slice_sync(
        db: sqlite3.Connection,
        aliases: list[str],
        family: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        selectors = list(dict.fromkeys(aliases + [f"family:{family}"]))
        if not selectors:
            return []
        placeholders = ",".join("?" for _ in selectors)
        rows = db.execute(
            f"""SELECT e.source_alias, e.target_alias, e.relation, e.observations, e.last_seen,
                       sn.kind, tn.kind
                FROM topology_edges AS e
                LEFT JOIN topology_nodes AS sn ON sn.alias = e.source_alias
                LEFT JOIN topology_nodes AS tn ON tn.alias = e.target_alias
                WHERE e.source_alias IN ({placeholders}) OR e.target_alias IN ({placeholders})
                ORDER BY e.observations DESC, e.last_seen DESC
                LIMIT ?""",
            tuple(selectors + selectors + [max(1, int(limit))]),
        ).fetchall()
        return [
            {
                "source": str(row[0]),
                "target": str(row[1]),
                "relation": str(row[2]),
                "observations": int(row[3]),
                "last_seen": float(row[4]),
                "source_kind": str(row[5] or "unknown"),
                "target_kind": str(row[6] or "unknown"),
            }
            for row in rows
        ]

    async def memory_health(self, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp())
        async with self._lock:
            return await asyncio.to_thread(self._memory_health_sync, now)

    def _memory_health_sync(self, now: float) -> dict[str, Any]:
        with sqlite3.connect(self.path) as db:
            total = int(db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0])
            expired = int(
                db.execute("SELECT COUNT(*) FROM knowledge WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)).fetchone()[0]
            )
            trust_rows = db.execute(
                "SELECT trust_class, COUNT(*) FROM knowledge GROUP BY trust_class ORDER BY trust_class"
            ).fetchall()
            outcome_rows = db.execute(
                "SELECT outcome, COUNT(*) FROM knowledge GROUP BY outcome ORDER BY outcome"
            ).fetchall()
            nodes = int(db.execute("SELECT COUNT(*) FROM topology_nodes").fetchone()[0])
            edges = int(db.execute("SELECT COUNT(*) FROM topology_edges").fetchone()[0])
            aliases = int(db.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0])
            outcomes = int(db.execute("SELECT COUNT(*) FROM outcome_history").fetchone()[0])
        return {
            "knowledge_total": total,
            "knowledge_active": max(0, total - expired),
            "knowledge_expired": expired,
            "by_trust": {str(name): int(count) for name, count in trust_rows},
            "by_outcome": {str(name): int(count) for name, count in outcome_rows},
            "stable_entity_aliases": aliases,
            "topology_nodes": nodes,
            "topology_edges": edges,
            "outcome_events": outcomes,
            "fts5_available": bool(self.fts_available),
        }

    async def mark_analysis_attempt(self, fp: str, now_ts: float | None = None) -> None:
        now = now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp()
        async with self._lock:
            await asyncio.to_thread(self._mark_analysis_attempt_sync, fp, now)

    def _mark_analysis_attempt_sync(self, fp: str, now: float) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE incidents SET last_analysis_at = ? WHERE fingerprint = ?", (float(now), fp))
            db.commit()

    async def reserve_ai_usage(
        self,
        *,
        fingerprint: str,
        provider: str,
        model: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_usd: float,
        monthly_stop_usd: float,
        now_ts: float | None = None,
        family: str = "",
    ) -> tuple[int | None, float]:
        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_ai_usage_sync,
                fingerprint, provider, model, reserved_input_tokens, reserved_output_tokens,
                reserved_cost_usd, monthly_stop_usd, now_ts, family,
            )

    def _reserve_ai_usage_sync(
        self,
        fingerprint: str,
        provider: str,
        model: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_usd: float,
        monthly_stop_usd: float,
        now_ts: float | None,
        family: str,
    ) -> tuple[int | None, float]:
        now = now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp()
        month_start, month_end, _ = month_bounds_utc(now)
        safe_family = str(family or "unknown")[:200]
        with sqlite3.connect(self.path) as db:
            spent = float(
                db.execute(
                    """SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage
                    WHERE ts >= ? AND ts < ? AND status != 'blocked_budget'""",
                    (month_start, month_end),
                ).fetchone()[0]
            )
            if spent + reserved_cost_usd > monthly_stop_usd + 1e-12:
                db.execute(
                    """INSERT INTO ai_usage
                    (ts, fingerprint, provider, model, family, status, reserved_input_tokens,
                     reserved_output_tokens, reserved_cost_usd, cost_usd, error)
                    VALUES (?, ?, ?, ?, ?, 'blocked_budget', ?, ?, ?, 0, ?)""",
                    (
                        now, fingerprint, provider, model, safe_family,
                        max(0, int(reserved_input_tokens)), max(0, int(reserved_output_tokens)),
                        max(0.0, float(reserved_cost_usd)),
                        f"monthly stop ${monthly_stop_usd:.8f} would be exceeded",
                    ),
                )
                db.commit()
                return None, spent
            cursor = db.execute(
                """INSERT INTO ai_usage
                (ts, fingerprint, provider, model, family, status, reserved_input_tokens,
                 reserved_output_tokens, reserved_cost_usd, cost_usd)
                VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)""",
                (
                    now, fingerprint, provider, model, safe_family,
                    max(0, int(reserved_input_tokens)), max(0, int(reserved_output_tokens)),
                    max(0.0, float(reserved_cost_usd)), max(0.0, float(reserved_cost_usd)),
                ),
            )
            db.commit()
            return int(cursor.lastrowid), spent

    async def finalize_ai_usage(
        self,
        usage_id: int,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(self._finalize_ai_usage_sync, usage_id, input_tokens, output_tokens, cost_usd)

    def _finalize_ai_usage_sync(self, usage_id: int, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """UPDATE ai_usage SET status = 'succeeded', input_tokens = ?, output_tokens = ?,
                    cost_usd = ?, error = NULL WHERE id = ?""",
                (max(0, int(input_tokens)), max(0, int(output_tokens)), max(0.0, float(cost_usd)), int(usage_id)),
            )
            db.commit()

    async def fail_ai_usage(self, usage_id: int, error: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._fail_ai_usage_sync, usage_id, error)

    def _fail_ai_usage_sync(self, usage_id: int, error: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE ai_usage SET status = 'failed', error = ? WHERE id = ?", (str(error)[:1000], int(usage_id)))
            db.commit()

    async def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_recent_sync, limit)

    def _list_recent_sync(self, limit: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM incidents ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    async def ai_count_since(self, since_ts: float) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._ai_count_since_sync, since_ts)

    def _ai_count_since_sync(self, since_ts: float) -> int:
        with sqlite3.connect(self.path) as db:
            return int(db.execute("SELECT COUNT(*) FROM ai_usage WHERE ts >= ?", (since_ts,)).fetchone()[0])

    async def ai_count_for_family_since(self, family: str, since_ts: float) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._ai_count_for_family_since_sync, family, since_ts)

    def _ai_count_for_family_since_sync(self, family: str, since_ts: float) -> int:
        with sqlite3.connect(self.path) as db:
            return int(
                db.execute("SELECT COUNT(*) FROM ai_usage WHERE family = ? AND ts >= ?", (str(family), float(since_ts))).fetchone()[0]
            )

    async def ai_family_counts_since(self, since_ts: float) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._ai_family_counts_since_sync, since_ts)

    def _ai_family_counts_since_sync(self, since_ts: float) -> dict[str, int]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                """SELECT family, COUNT(*) FROM ai_usage
                WHERE ts >= ? AND family != '' GROUP BY family
                ORDER BY COUNT(*) DESC, family ASC LIMIT 20""",
                (float(since_ts),),
            ).fetchall()
        return {str(family): int(count) for family, count in rows}

    async def monthly_ai_usage(self, now_ts: float | None = None) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._monthly_ai_usage_sync, now_ts)

    def _monthly_ai_usage_sync(self, now_ts: float | None) -> dict[str, Any]:
        now = now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp()
        month_start, month_end, month = month_bounds_utc(now)
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                """SELECT
                    COALESCE(SUM(CASE WHEN status != 'blocked_budget' THEN cost_usd ELSE 0 END), 0),
                    SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status IN ('reserved','succeeded','failed','legacy_success') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'blocked_budget' THEN 1 ELSE 0 END),
                    COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                FROM ai_usage WHERE ts >= ? AND ts < ?""",
                (month_start, month_end),
            ).fetchone()
        return {
            "month_utc": month,
            "spent_usd": float(row[0] or 0),
            "analyses_count": int(row[1] or 0),
            "attempts_count": int(row[2] or 0),
            "failed_count": int(row[3] or 0),
            "reserved_count": int(row[4] or 0),
            "budget_blocked_count": int(row[5] or 0),
            "input_tokens": int(row[6] or 0),
            "output_tokens": int(row[7] or 0),
        }
