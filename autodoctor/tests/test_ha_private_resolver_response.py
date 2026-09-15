from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.ha import HomeAssistantClient


class ResolverSocket:
    def __init__(self, response):
        self.responses = iter([{"type": "auth_required"}, {"type": "auth_ok"}, response])
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def receive_json(self):
        return next(self.responses)

    async def send_json(self, payload):
        self.sent.append(payload)


async def resolve_response(response):
    ws = ResolverSocket(response)
    client = HomeAssistantClient.__new__(HomeAssistantClient)
    client.token = "test-token-not-real"
    client.ws_url = "ws://supervisor/core/websocket"
    client.session = SimpleNamespace(ws_connect=lambda *_args, **_kwargs: ws)
    result = await client.match_tplink_config_entries_by_host("192.168.50.10")
    assert ws.sent == [
        {"type": "auth", "access_token": "test-token-not-real"},
        {
            "id": 1,
            "type": "autodoctor_private_resolver/match_tplink_host",
            "host": "192.168.50.10",
        },
    ]
    return result


def test_resolver_returns_only_validated_match_fields():
    response = {
        "type": "result",
        "success": True,
        "result": {
            "domain": "tplink",
            "count": 1,
            "host": "192.168.50.10",
            "matches": [
                {"entry_id": "  entry_test123  ", "state": "loaded", "host": "192.168.50.10"}
            ],
        },
    }
    assert asyncio.run(resolve_response(response)) == {
        "domain": "tplink",
        "count": 1,
        "matches": [{"entry_id": "entry_test123", "state": "loaded"}],
    }


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"domain": "other", "count": 0, "matches": []},
        {"domain": "tplink", "count": 0, "matches": {}},
        {"domain": "tplink", "count": 1, "matches": ["entry_test123"]},
        {"domain": "tplink", "count": 1, "matches": [{"entry_id": "invalid/id"}]},
        {"domain": "tplink", "count": 1, "matches": [{}]},
        {"domain": "tplink", "count": "invalid", "matches": []},
        {"domain": "tplink", "count": 1, "matches": []},
    ],
)
def test_resolver_rejects_malformed_or_inconsistent_matches(result):
    with pytest.raises(RuntimeError, match="private TP-Link resolver"):
        asyncio.run(resolve_response({"type": "result", "success": True, "result": result}))


@pytest.mark.parametrize(
    "response",
    [{"type": "event", "success": True}, {"type": "result", "success": False}],
)
def test_resolver_rejects_unsuccessful_responses(response):
    with pytest.raises(RuntimeError, match="unavailable or rejected"):
        asyncio.run(resolve_response(response))
