from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from autodoctor.context import sanitize_state_value
from autodoctor.dashboard import ingress_remote_allowed
from autodoctor.ha import (
    _HA_HTTP_TIMEOUT,
    _HA_WS_HANDSHAKE_TIMEOUT_SECONDS,
    _HA_WS_TIMEOUT,
)
from autodoctor.models import LogEvent
from autodoctor.store import IncidentStore


def test_ingress_accepts_only_supervisor_proxy() -> None:
    assert ingress_remote_allowed("172.30.32.2")
    assert not ingress_remote_allowed("127.0.0.1")
    assert not ingress_remote_allowed("172.30.32.3")
    assert not ingress_remote_allowed(None)


def test_home_assistant_network_operations_are_bounded() -> None:
    assert _HA_HTTP_TIMEOUT.total == 30
    assert _HA_HTTP_TIMEOUT.connect == 10
    assert _HA_HTTP_TIMEOUT.sock_read == 20
    assert _HA_WS_TIMEOUT.ws_receive is None
    assert _HA_WS_TIMEOUT.ws_close == 10
    assert _HA_WS_HANDSHAKE_TIMEOUT_SECONDS == 15


def test_state_sanitizer_redacts_sensitive_state_shapes() -> None:
    aliases: dict[str, str] = {}
    assert sanitize_state_value("device_tracker.phone", "home", aliases) == "home"
    assert (
        sanitize_state_value("device_tracker.phone", "Private Workplace", aliases)
        == "<REDACTED_LOCATION_STATE>"
    )
    assert sanitize_state_value("input_text.note", "personal text", aliases) == "<REDACTED_TEXT_STATE>"
    sanitized = sanitize_state_value(
        "sensor.status",
        "token=secret-value user@example.com 192.168.1.5",
        aliases,
    )
    assert "secret-value" not in sanitized
    assert "user@example.com" not in sanitized
    assert "192.168.1.5" not in sanitized


def test_app_does_not_request_unused_supervisor_api_permission() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert config["homeassistant_api"] is True
    assert "hassio_api" not in config
    assert "hassio_role" not in config


def test_incident_retention_and_open_count_are_bounded(tmp_path: Path) -> None:
    async def run() -> None:
        store = IncidentStore(str(tmp_path / "retention.db"), max_incidents_retained=3)
        await store.initialize()
        for index in range(5):
            event = LogEvent(
                level="ERROR",
                source="test.py",
                exception="",
                message=f"incident {index}",
                name="homeassistant.test",
                timestamp=float(index + 1),
            )
            await store.record(f"fp-{index}", event)
        rows = await store.list_recent(10)
        assert [row["fingerprint"] for row in rows] == ["fp-4", "fp-3", "fp-2"]
        assert await store.open_incident_count() == 3

    asyncio.run(run())


def test_budget_blocks_do_not_consume_hourly_attempt_caps(tmp_path: Path) -> None:
    async def run() -> None:
        store = IncidentStore(str(tmp_path / "budget.db"))
        await store.initialize()
        blocked_id, _ = await store.reserve_ai_usage(
            fingerprint="blocked",
            provider="openai",
            model="test",
            family="family-a",
            reserved_input_tokens=100,
            reserved_output_tokens=100,
            reserved_cost_usd=1.0,
            monthly_stop_usd=0.0,
            now_ts=1000.0,
        )
        assert blocked_id is None
        assert await store.ai_count_since(999.0) == 0
        assert await store.ai_count_for_family_since("family-a", 999.0) == 0
        assert await store.ai_family_counts_since(999.0) == {}

        usage_id, _ = await store.reserve_ai_usage(
            fingerprint="real-attempt",
            provider="openai",
            model="test",
            family="family-a",
            reserved_input_tokens=100,
            reserved_output_tokens=100,
            reserved_cost_usd=0.1,
            monthly_stop_usd=10.0,
            now_ts=1001.0,
        )
        assert usage_id is not None
        assert await store.ai_count_since(999.0) == 1
        assert await store.ai_count_for_family_since("family-a", 999.0) == 1
        assert await store.ai_family_counts_since(999.0) == {"family-a": 1}

    asyncio.run(run())
