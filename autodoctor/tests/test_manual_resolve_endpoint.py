from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import make_mocked_request

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor.repair_dashboard import RepairDashboard


class FakeCases:
    def __init__(self, status: str) -> None:
        self.status = status
        self.resolved: list[str] = []

    async def get_case(self, pattern_key: str):
        return {"pattern_key": pattern_key, "status": self.status}

    async def mark_resolved(self, pattern_key: str, *, verification: str = "") -> None:
        _ = verification
        self.resolved.append(pattern_key)


class FakeRequest:
    content_type = "application/x-www-form-urlencoded"

    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload

    async def post(self):
        return self.payload


def _dashboard(status: str) -> RepairDashboard:
    dashboard = object.__new__(RepairDashboard)
    dashboard.executor = SimpleNamespace(approval_nonce="nonce", enabled=True)
    dashboard.engine = SimpleNamespace(cases=FakeCases(status))
    return dashboard


def test_manual_resolve_allows_safe_nonexecuting_case() -> None:
    async def run() -> None:
        dashboard = _dashboard("diagnosed")
        request = FakeRequest({"approval_nonce": "nonce", "pattern_key": "demo/x"})
        try:
            await dashboard.resolve_case(request)  # type: ignore[arg-type]
        except Exception as exc:
            # Successful form handling ends in HTTPSeeOther.
            assert getattr(exc, "status", None) == 303
        assert dashboard.engine.cases.resolved == ["demo/x"]

    asyncio.run(run())


def test_manual_resolve_cannot_hide_repair_or_verification_state() -> None:
    async def run() -> None:
        for status in ("investigating", "repair_available", "verifying"):
            dashboard = _dashboard(status)
            request = FakeRequest({"approval_nonce": "nonce", "pattern_key": "demo/x"})
            try:
                await dashboard.resolve_case(request)  # type: ignore[arg-type]
            except Exception as exc:
                assert getattr(exc, "status", None) == 409
            else:
                raise AssertionError(f"{status} should be protected")
            assert dashboard.engine.cases.resolved == []

    asyncio.run(run())


def test_manual_resolve_requires_process_nonce() -> None:
    async def run() -> None:
        dashboard = _dashboard("diagnosed")
        request = FakeRequest({"approval_nonce": "wrong", "pattern_key": "demo/x"})
        try:
            await dashboard.resolve_case(request)  # type: ignore[arg-type]
        except Exception as exc:
            assert getattr(exc, "status", None) == 403
        else:
            raise AssertionError("invalid nonce should be rejected")
        assert dashboard.engine.cases.resolved == []

    asyncio.run(run())
