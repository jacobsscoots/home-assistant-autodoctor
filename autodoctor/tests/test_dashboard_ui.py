from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor import AUTODOCTOR_VERSION
from autodoctor.dashboard_ui import render_dashboard


class FakeExecutor:
    enabled = True

    @staticmethod
    def validate_plan(plan):
        if plan.get("repair_type") == "reload_config_entry":
            return True, "ok", "private-target"
        return False, "unsupported repair", None


def _health() -> dict:
    return {
        "status": "healthy",
        "ai_budget": {
            "enabled": True,
            "spent_usd": 0.42,
            "stop_threshold_usd": 5.0,
            "remaining_to_stop_usd": 4.58,
            "analyses_count": 12,
        },
        "mcp": {"connected": True, "server_profile": "ha-mcp"},
        "case_management": {
            "cases_by_status": {
                "diagnosed": 1,
                "repair_available": 1,
                "resolved": 2,
                "historical": 3,
                "suppressed_nonfatal": 4,
            },
            "notification_lifecycle": {
                "notification_dismissals": 7,
                "notification_dismiss_failures": 0,
            },
            "nonfatal_observation_filter": {
                "enabled": True,
                "events_suppressed_since_start": 99,
            },
            "backlog_triage": {"enabled": True, "pending_cases": 0},
            "private_target_resolution": {"bindings": 0, "last_result": "not_requested"},
        },
    }


def test_dashboard_is_current_responsive_and_accessible() -> None:
    text = render_dashboard(
        health=_health(),
        incidents=[],
        cases=[],
        plans=[],
        executor_health={"enabled": True},
        executor=FakeExecutor(),
        approval_nonce="nonce",
    )
    assert f"AutoDoctor v{AUTODOCTOR_VERSION}" in text
    assert 'name="viewport"' in text
    assert 'id="main-content"' in text
    assert 'aria-label="System overview"' in text
    assert "prefers-color-scheme:dark" in text
    assert "prefers-reduced-motion:reduce" in text
    assert "Automatic repairs are off" in text
    assert "Private target identifiers are withheld from the AI" in text
    assert "v0.2.1" not in text
    assert "repair executor remains disabled" not in text


def test_dashboard_redacts_network_values_and_escapes_untrusted_evidence() -> None:
    incidents = [
        {
            "last_seen": 1.0,
            "occurrences": 4,
            "pattern_label": "timeout",
            "name": "kasa.protocol",
            "message": '<script>alert(1)</script> failed at 192.168.1.20 token=secret-value',
            "analysis_json": '{"risk":"low","summary":"Retry against 10.0.0.7"}',
        }
    ]
    text = render_dashboard(
        health=_health(),
        incidents=incidents,
        cases=[],
        plans=[],
        executor_health={"enabled": True},
        executor=FakeExecutor(),
        approval_nonce="nonce",
    )
    assert "192.168.1.20" not in text
    assert "10.0.0.7" not in text
    assert "secret-value" not in text
    assert "<IP>" not in text  # escaped HTML representation is expected instead
    assert "&lt;IP&gt;" in text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_manual_resolve_is_offered_only_for_safe_nonexecuting_statuses() -> None:
    cases = [
        {
            "pattern_key": "safe/diagnosed/x",
            "pattern_label": "safe diagnosed",
            "family": "safe",
            "status": "diagnosed",
            "last_seen": 10,
            "occurrences": 2,
            "fingerprint_count": 1,
        },
        {
            "pattern_key": "unsafe/repair/x",
            "pattern_label": "repair pending",
            "family": "unsafe",
            "status": "repair_available",
            "last_seen": 11,
            "occurrences": 1,
            "fingerprint_count": 1,
        },
        {
            "pattern_key": "unsafe/investigating/x",
            "pattern_label": "investigating now",
            "family": "unsafe",
            "status": "investigating",
            "last_seen": 12,
            "occurrences": 1,
            "fingerprint_count": 1,
        },
    ]
    text = render_dashboard(
        health=_health(),
        incidents=[],
        cases=cases,
        plans=[],
        executor_health={"enabled": True},
        executor=FakeExecutor(),
        approval_nonce="nonce-value",
    )
    assert text.count("Mark resolved & dismiss") == 1
    assert 'value="safe/diagnosed/x"' in text
    assert 'value="unsafe/repair/x"' not in text
    assert 'value="unsafe/investigating/x"' not in text
    assert 'value="nonce-value"' in text


def test_repair_plan_approval_is_shown_only_after_executor_validation() -> None:
    plans = [
        {
            "plan_id": "plan_safe",
            "status": "proposed",
            "risk": "low",
            "confidence": 0.96,
            "repair_type": "reload_config_entry",
            "summary": "Reload one entry",
        },
        {
            "plan_id": "plan_bad",
            "status": "proposed",
            "risk": "low",
            "confidence": 0.96,
            "repair_type": "manual_review",
            "summary": "Unsupported mutation",
        },
    ]
    text = render_dashboard(
        health=_health(),
        incidents=[],
        cases=[],
        plans=plans,
        executor_health={"enabled": True},
        executor=FakeExecutor(),
        approval_nonce="nonce",
    )
    assert text.count("Approve one config-entry reload") == 1
    assert "Not executable: unsupported repair" in text
    assert text.count("Reject plan") == 2
