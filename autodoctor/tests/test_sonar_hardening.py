from __future__ import annotations

from autodoctor.cases import IncidentCaseManager


def test_backlog_row_aggregation_keeps_latest_representative() -> None:
    groups: dict[str, dict] = {}
    first = {
        "fingerprint": "fp-old",
        "pattern_key": "kasa/authentication/example",
        "pattern_label": "authentication",
        "first_seen": 10.0,
        "last_seen": 20.0,
        "occurrences": 3,
    }
    latest = {
        "fingerprint": "fp-new",
        "pattern_key": "kasa/authentication/example",
        "pattern_label": "authentication",
        "first_seen": 15.0,
        "last_seen": 30.0,
        "occurrences": 4,
    }

    assert IncidentCaseManager._aggregate_backlog_row(groups, first) == "fp-old"
    assert IncidentCaseManager._aggregate_backlog_row(groups, latest) == "fp-new"

    case = groups["kasa/authentication/example"]
    assert case["first_seen"] == 10.0
    assert case["last_seen"] == 30.0
    assert case["occurrences"] == 7
    assert case["fingerprints"] == 2
    assert case["representative_fingerprint"] == "fp-new"
