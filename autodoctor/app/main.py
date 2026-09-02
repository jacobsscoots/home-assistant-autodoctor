from __future__ import annotations

import asyncio
import logging

from autodoctor.case_engine import CaseAwareAutoDoctorEngine
from autodoctor.config import Settings
from autodoctor.dashboard import Dashboard
from autodoctor.ha import HomeAssistantClient
from autodoctor.llm import build_provider
from autodoctor.mcp_backend import MCPBackend
from autodoctor.seed_store import SeedAwareIncidentStore
from autodoctor.transport_logging import suppress_sensitive_http_transport_logs


async def async_main() -> None:
    settings = Settings.load()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [autodoctor] %(message)s",
    )
    suppress_sensitive_http_transport_logs()

    store = SeedAwareIncidentStore(
        "/data/autodoctor.db",
        seed_enabled=settings.memory_seed_enabled,
        max_incidents_retained=settings.max_incidents_retained,
    )
    ha = HomeAssistantClient()
    llm = build_provider(settings)
    mcp = MCPBackend(settings)
    engine = CaseAwareAutoDoctorEngine(settings, store, ha, llm, mcp)
    dashboard = Dashboard(settings, store, engine)

    await store.initialize()
    await mcp.start()
    reconciliation = await engine.initialize_case_management()
    logging.getLogger(__name__).info(
        "Case backlog reconciliation complete: cases=%s legacy_notifications_dismissed=%s",
        reconciliation.get("cases", 0),
        reconciliation.get("legacy_notifications_dismissed", 0),
    )
    await dashboard.start()

    try:
        await engine.run_forever()
    finally:
        await dashboard.stop()
        await mcp.close()
        await ha.close()
        await llm.close()


if __name__ == "__main__":
    asyncio.run(async_main())
