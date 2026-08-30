from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from .fingerprint import normalize_for_fingerprint
from .models import Analysis, LogEvent
from .redact import redact

TRUST_SCORES: dict[str, float] = {
    "observed": 0.35,
    "ai_hypothesis": 0.45,
    "manually_verified": 0.85,
    "verified_fix": 1.0,
    "failed_fix": 0.15,
    "deprecated": 0.0,
}

DEFAULT_EXPIRY_DAYS: dict[str, int] = {
    "observed": 30,
    "ai_hypothesis": 30,
    "manually_verified": 180,
    "verified_fix": 365,
    "failed_fix": 90,
    "deprecated": 1,
}

_ENTITY = re.compile(r"\b[a-z_]+\.[a-zA-Z0-9_]+\b")
_TOKEN = re.compile(r"[a-zA-Z0-9_]{3,}")


def trust_score(trust_class: str) -> float:
    return float(TRUST_SCORES.get(str(trust_class), 0.0))


def expiry_timestamp(trust_class: str, created_at: float, override_days: int | None = None) -> float:
    days = int(override_days if override_days is not None else DEFAULT_EXPIRY_DAYS.get(trust_class, 30))
    return float(created_at) + max(1, days) * 86400.0


def _pattern_label(text: str) -> str:
    lower = text.lower()
    if "timeout" in lower and any(word in lower for word in ("query", "update", "module", "device")):
        return "device_query_timeout"
    if "timeout" in lower:
        return "timeout"
    if "connection refused" in lower or "connect call failed" in lower:
        return "connection_refused"
    if "429" in lower or "rate limit" in lower or "too many requests" in lower:
        return "rate_limit"
    if any(word in lower for word in ("401", "403", "unauthorized", "unauthorised", "authentication failed")):
        return "authentication"
    if "template" in lower and any(word in lower for word in ("error", "undefined", "render")):
        return "template_error"
    if any(word in lower for word in ("not found", "does not exist", "unknown service", "unknown entity")):
        return "not_found"
    if "unavailable" in lower:
        return "unavailable"
    if any(word in lower for word in ("sqlite", "recorder", "database")):
        return "storage"
    if any(word in lower for word in ("json", "decode", "parse")) and "error" in lower:
        return "parse_error"
    return "other"


def pattern_signature(event: LogEvent, family: str) -> tuple[str, str]:
    """Return a broader failure-class key than the exact incident fingerprint."""
    exception_head = event.exception.splitlines()[0] if event.exception else ""
    combined = f"{event.message}\n{exception_head}"
    label = _pattern_label(combined)
    if label == "other":
        broad = normalize_for_fingerprint(_ENTITY.sub("<ENTITY>", combined))[:400]
        material = f"{family}|{label}|{broad}"
    else:
        # Recognised failure classes intentionally group changing device IDs,
        # addresses, module lists and timestamps into one local memory pattern.
        material = f"{family}|{label}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]
    return f"{family}/{label}/{digest}", label


def fts_query(text: str, family: str = "", pattern_label: str = "") -> str:
    raw = " ".join((family, pattern_label, text))
    ignored = {
        "homeassistant",
        "error",
        "failed",
        "failure",
        "exception",
        "entity",
        "sensor",
        "switch",
        "automation",
    }
    terms: list[str] = []
    for token in _TOKEN.findall(raw.lower()):
        if token in ignored or token.isdigit() or token in terms:
            continue
        terms.append(token)
        if len(terms) >= 12:
            break
    return " OR ".join(f'"{term}"' for term in terms)


def effective_score(item: dict[str, Any], *, family: str, pattern_key: str, now: float) -> float:
    if item.get("trust_class") == "deprecated" or item.get("superseded_by"):
        return -1e9
    expires_at = float(item.get("expires_at") or 0)
    if expires_at and expires_at <= now:
        return -1e9

    score = float(item.get("trust_score") or trust_score(str(item.get("trust_class") or ""))) * 40.0
    if pattern_key and item.get("pattern_key") == pattern_key:
        score += 45.0
    if family and item.get("family") == family:
        score += 18.0

    last = float(item.get("last_confirmed_at") or item.get("verified_at") or item.get("created_at") or 0)
    if last:
        age_days = max(0.0, (now - last) / 86400.0)
        score += max(0.0, 12.0 - min(12.0, age_days / 30.0))

    outcome = str(item.get("outcome") or "")
    if outcome in {"fixed_manual", "fixed_verified"}:
        score += 8.0
    elif outcome in {"failed", "worsened"}:
        score -= 4.0
    return score


def safe_memory_item(item: dict[str, Any]) -> dict[str, Any]:
    """Build the only shape allowed to leave the local memory store for an LLM prompt."""
    return {
        "memory_key": str(item.get("memory_key") or ""),
        "family": str(item.get("family") or "unknown"),
        "pattern_key": str(item.get("pattern_key") or ""),
        "pattern_label": str(item.get("pattern_label") or ""),
        "trust_class": str(item.get("trust_class") or "observed"),
        "trust_score": round(float(item.get("trust_score") or 0.0), 3),
        "source": str(item.get("source") or ""),
        "root_cause": redact(str(item.get("root_cause") or ""))[:1800],
        "resolution": redact(str(item.get("resolution") or ""))[:2200],
        "verification": redact(str(item.get("verification") or ""))[:1800],
        "outcome": str(item.get("outcome") or "unknown"),
        "recurrence_count": int(item.get("recurrence_count") or 0),
        "ha_version": str(item.get("ha_version") or "unknown"),
        "autodoctor_version": str(item.get("autodoctor_version") or "unknown"),
        "verified_at": item.get("verified_at"),
        "last_confirmed_at": item.get("last_confirmed_at"),
        "expires_at": item.get("expires_at"),
    }


def bounded_memory(items: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    remaining = max(0, int(max_chars))
    output: list[dict[str, Any]] = []
    for item in items:
        safe = safe_memory_item(item)
        encoded = json.dumps(safe, separators=(",", ":"), ensure_ascii=False)
        if len(encoded) > remaining:
            if not output and remaining >= 400:
                # Keep one useful memory rather than returning nothing. Trim only
                # narrative fields; provenance/trust metadata stays intact.
                for field in ("resolution", "verification", "root_cause"):
                    value = str(safe.get(field) or "")
                    if len(value) > 300:
                        safe[field] = value[:300] + "…"
                encoded = json.dumps(safe, separators=(",", ":"), ensure_ascii=False)
                if len(encoded) <= remaining:
                    output.append(safe)
            break
        output.append(safe)
        remaining -= len(encoded)
    return output


def analysis_to_memory_text(analysis: Analysis) -> tuple[str, str]:
    resolution = json.dumps(analysis.proposed_changes, separators=(",", ":"), ensure_ascii=False)
    verification = json.dumps(analysis.checks, separators=(",", ":"), ensure_ascii=False)
    return redact(resolution)[:4000], redact(verification)[:3000]


def utc_timestamp(value: str) -> float:
    text = value.strip()
    if len(text) == 10:
        text += "T12:00:00+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()


SEED_KNOWLEDGE: tuple[dict[str, Any], ...] = (
    {
        "memory_key": "seed:icloud3-live-entity-resolution",
        "family": "icloud3",
        "pattern_label": "not_found",
        "trust_class": "verified_fix",
        "source": "manual-pre-autodoctor github#7,#8",
        "root_cause": "A malformed device-tracker slug referenced an entity that did not exist in the live registry/state machine.",
        "resolution": "Resolve the intended tracker against live Home Assistant first, then replace only the malformed references with the verified current entity. Never infer the target from a stale YAML snapshot.",
        "verification": "The intended iCloud3 trackers existed live and were enabled; malformed references existed only in affected automation configuration.",
        "outcome": "fixed_manual",
        "verified_at": "2026-08-01",
    },
    {
        "memory_key": "seed:sleep-wake-reconciliation",
        "family": "homeassistant",
        "pattern_label": "automation_state",
        "trust_class": "verified_fix",
        "source": "manual-pre-autodoctor github#9",
        "root_cause": "Sleep/wake state transitions relied on an insufficiently corroborated path and could fail to reconcile the intended home state.",
        "resolution": "Use explicit evidence-based wake branches with bounded timing, interior presence corroboration, sleep duration and away-state reconciliation. Presence/sleep behavior remains approval-required.",
        "verification": "Post-audit configuration was healthy and the hardened reconciliation branches were present.",
        "outcome": "fixed_manual",
        "verified_at": "2026-08-24",
    },
    {
        "memory_key": "seed:morning-tapo-timeout",
        "family": "kasa",
        "pattern_label": "device_query_timeout",
        "trust_class": "verified_fix",
        "source": "manual-pre-autodoctor github#10",
        "root_cause": "A slow or unavailable Tapo operation sat too close to critical morning-routine control flow.",
        "resolution": "Bound the device timeout and isolate failure of the network/device path so it cannot derail the wider routine.",
        "verification": "The restructured timeout path was included in the verified post-audit configuration.",
        "outcome": "fixed_manual",
        "verified_at": "2026-08-24",
    },
    {
        "memory_key": "seed:dishwasher-rearm",
        "family": "homeassistant",
        "pattern_label": "automation_state",
        "trust_class": "verified_fix",
        "source": "manual-pre-autodoctor github#11",
        "root_cause": "Dishwasher completion logic could leave residual state that reduced reliability of the next cycle.",
        "resolution": "Handle terminal completion and re-arming explicitly so the automation returns to a known armed state.",
        "verification": "The corrected finish/re-arm logic was present in the verified post-audit configuration.",
        "outcome": "fixed_manual",
        "verified_at": "2026-08-24",
    },
    {
        "memory_key": "seed:pc-power-safety",
        "family": "homeassistant",
        "pattern_label": "power_safety",
        "trust_class": "verified_fix",
        "source": "manual-pre-autodoctor github#12",
        "root_cause": "A missing or ambiguous operating-system shutdown signal could otherwise tempt automation logic to substitute mains power removal.",
        "resolution": "Never cut a shared smart-plug/power strip as a substitute for confirmed OS shutdown. Gate physical power removal on positive evidence that the computer is already safely off.",
        "verification": "The audited PC sleep path was reduced to notification/observation behavior and configuration remained healthy.",
        "outcome": "fixed_manual",
        "verified_at": "2026-08-24",
    },
    {
        "memory_key": "seed:presence-arrival-window",
        "family": "icloud3",
        "pattern_label": "timing_skew",
        "trust_class": "verified_fix",
        "source": "manual-pre-autodoctor github#13",
        "root_cause": "Arrival evidence from independent integrations could arrive a few seconds out of order and be rejected by an overly narrow window.",
        "resolution": "Allow a tightly bounded pre-event evidence window so legitimate near-simultaneous arrival evidence is retained while unrelated stale evidence remains excluded.",
        "verification": "A live arrival demonstrated the network/home transition on the same event boundary and was accepted by the widened window.",
        "outcome": "fixed_manual",
        "verified_at": "2026-08-16",
    },
    {
        "memory_key": "seed:high-confidence-home-trigger-fanout",
        "family": "homeassistant",
        "pattern_label": "automation_state",
        "trust_class": "manually_verified",
        "source": "manual-pre-autodoctor github#14",
        "root_cause": "Presence synchronisation listened to more triggers than necessary, creating redundant evaluations and harder-to-audit transitions.",
        "resolution": "Reduce the trigger set to the minimum explicit evidence sources required by the high-confidence-home state machine.",
        "verification": "The reduced-trigger version was present in the healthy post-audit configuration and diagnostic manifest.",
        "outcome": "fixed_manual",
        "verified_at": "2026-08-24",
    },
)


def seed_payload(record: dict[str, Any], *, now: float) -> dict[str, Any]:
    verified_at = utc_timestamp(str(record["verified_at"]))
    trust_class = str(record["trust_class"])
    return {
        **record,
        "trust_score": trust_score(trust_class),
        "created_at": verified_at,
        "verified_at": verified_at,
        "last_confirmed_at": verified_at,
        "expires_at": expiry_timestamp(trust_class, verified_at),
        "pattern_key": "",
        "fingerprint": "",
        "ha_version": "unknown",
        "autodoctor_version": "pre-autodoctor",
        "recurrence_count": 0,
        "baseline_occurrences": 0,
        "metadata_json": "{}",
        "superseded_by": None,
    }
