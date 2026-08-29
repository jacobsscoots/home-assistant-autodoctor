from __future__ import annotations

import asyncio
import logging

from autodoctor.config import Settings
from autodoctor.dashboard import Dashboard
from autodoctor.engine import AutoDoctorEngine
from autodoctor.ha import HomeAssistantClient
from autodoctor.llm import build_provider
from autodoctor.mcp_backend import MCPBackend
from autodoctor.store import IncidentStore


async def async_main() -> None:
    settings = Settings.load()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [autodoctor] %(message)s",
    )

    store = IncidentStore("/data/autodoctor.db")
    ha = HomeAssistantClient()
    llm = build_provider(settings)
    mcp = MCPBackend(settings)
    engine = AutoDoctorEngine(settings, store, ha, llm, mcp)
    dashboard = Dashboard(settings, store, engine)

    await store.initialize()
    await dashboard.start()

    try:
        await engine.run_forever()
    finally:
        await dashboard.stop()
        await ha.close()
        await llm.close()


if __name__ == "__main__":
    asyncio.run(async_main())
