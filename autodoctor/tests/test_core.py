from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.context import collect_context
from autodoctor.fingerprint import fingerprint
from autodoctor.llm import AnthropicProvider, OpenAIProvider
from autodoctor.models import LogEvent
from autodoctor.policy import can_auto_apply, is_immediate, looks_transient, should_ignore
from autodoctor.redact import redact


def event(message: str, exception: str = "", name: str = "homeassistant.test") -> LogEvent:
    return LogEvent("ERROR", "core.py", exception, message, name, 1.0)


def test_fingerprint_ignores_incidental_numbers() -> None:
    a = fingerprint(event("Request failed after 10 seconds on attempt 1"))
    b = fingerprint(event("Request failed after 20 seconds on attempt 2"))
    assert a == b


def test_fingerprint_keeps_entity_identity() -> None:
    a = fingerprint(event("sensor.kitchen_temp unavailable"))
    b = fingerprint(event("sensor.bedroom_temp unavailable"))
    assert a != b


def test_redaction() -> None:
    text = "token=abc123 email me@example.com at 192.168.1.2"
    value = redact(text)
    assert "abc123" not in value
    assert "me@example.com" not in value
    assert "192.168.1.2" not in value


def test_policy_immediate_and_transient() -> None:
    assert is_immediate(event("Setup failed for integration foo"))
    assert looks_transient(event("Connection timed out"))


def test_feedback_loop_filter_ignores_autodoctor_events() -> None:
    assert should_ignore(event("AutoDoctor emitted this diagnostic message"))
    assert should_ignore(event("ordinary message", name="autodoctor.worker"))
    assert not should_ignore(event("Synthetic monitor-only pipeline verification event"))


def test_context_pseudonymizes_entities_before_external_ai() -> None:
    class FakeHA:
        async def get_state(self, entity_id: str):
            assert entity_id == "device_tracker.private_phone"
            return {
                "state": "home",
                "last_changed": "2026-08-29T18:00:00+00:00",
                "attributes": {
                    "friendly_name": "Private Phone Owner",
                    "device_class": None,
                },
            }

    value = asyncio.run(
        collect_context(
            event("device_tracker.private_phone became unavailable"),
            FakeHA(),
        )
    )
    serialized = json.dumps(value)

    assert "device_tracker.private_phone" not in serialized
    assert "Private Phone Owner" not in serialized
    assert "device_tracker.entity_1" in serialized
    assert value["referenced_entities"]["device_tracker.entity_1"]["state"] == "home"


def test_v01_never_auto_applies() -> None:
    assert not can_auto_apply("low", "deterministic_fix", True)


def test_openai_requires_explicit_model() -> None:
    with pytest.raises(RuntimeError, match="ai_model must be set explicitly"):
        OpenAIProvider("test-key", "", "low")


def test_anthropic_requires_explicit_model() -> None:
    with pytest.raises(RuntimeError, match="ai_model must be set explicitly"):
        AnthropicProvider("test-key", "", "low")


def test_mcp_v2_http_transport_imports() -> None:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    assert httpx2.AsyncClient is not None
    assert Client is not None
    assert streamable_http_client is not None
