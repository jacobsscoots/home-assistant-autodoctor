from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.fingerprint import fingerprint
from autodoctor.llm import AnthropicProvider, OpenAIProvider
from autodoctor.models import LogEvent
from autodoctor.policy import can_auto_apply, is_immediate, looks_transient
from autodoctor.redact import redact


def event(message: str, exception: str = "") -> LogEvent:
    return LogEvent("ERROR", "core.py", exception, message, "homeassistant.test", 1.0)


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


def test_v01_never_auto_applies() -> None:
    assert not can_auto_apply("low", "deterministic_fix", True)


def test_openai_requires_explicit_model() -> None:
    with pytest.raises(RuntimeError, match="ai_model must be set explicitly"):
        OpenAIProvider("test-key", "", "low")


def test_anthropic_requires_explicit_model() -> None:
    with pytest.raises(RuntimeError, match="ai_model must be set explicitly"):
        AnthropicProvider("test-key", "", "low")
