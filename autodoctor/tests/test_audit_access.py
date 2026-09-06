from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

import autodoctor.audit_access as access


def _request(*, remote: str, path: str, method: str = "GET", token: str = "token"):
    return SimpleNamespace(
        remote=remote,
        path=path,
        method=method,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )


def test_internal_addon_remote_scope() -> None:
    assert access.internal_addon_remote_allowed("172.30.33.20")
    assert access.internal_addon_remote_allowed("172.30.32.20")
    assert not access.internal_addon_remote_allowed("192.168.1.20")
    assert not access.internal_addon_remote_allowed("127.0.0.1")


def test_authenticated_internal_access_is_qualification_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        async def allow(_token: str) -> bool:
            return True

        async def handler(_request):
            return web.Response(text="ok")

        monkeypatch.setattr(access, "supervisor_token_has_core_access", allow)

        response = await access.ingress_or_authenticated_qualification(
            _request(remote="172.30.33.20", path="/api/qualification"), handler
        )
        assert response.status == 200

        with pytest.raises(web.HTTPForbidden):
            await access.ingress_or_authenticated_qualification(
                _request(remote="172.30.33.20", path="/api/cases"), handler
            )

        with pytest.raises(web.HTTPForbidden):
            await access.ingress_or_authenticated_qualification(
                _request(remote="192.168.1.20", path="/api/qualification"), handler
            )

    asyncio.run(run())


def test_invalid_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        async def deny(_token: str) -> bool:
            return False

        async def handler(_request):
            return web.Response(text="ok")

        monkeypatch.setattr(access, "supervisor_token_has_core_access", deny)
        with pytest.raises(web.HTTPForbidden):
            await access.ingress_or_authenticated_qualification(
                _request(remote="172.30.33.20", path="/api/qualification"), handler
            )

    asyncio.run(run())
