from __future__ import annotations

import asyncio
import logging

from autodoctor.ai_usage_recovery import recover_orphaned_ai_usage
from autodoctor.automatic_repair import AutoApplyRepairExecutor, AutomaticRepairCoordinator
from autodoctor.case_engine import CaseAwareAutoDoctorEngine
from autodoctor.config import Settings
from autodoctor.control_dashboard import ControlDashboard
from autodoctor.ha import HomeAssistantClient
from autodoctor.llm import build_provider
from autodoctor.log_filters import install_nonfatal_log_coalescing
from autodoctor.mcp_backend import MCPBackend
from autodoctor.seed_store import SeedAwareIncidentStore
from autodoctor.startup_recovery import recover_interrupted_case_investigations
from autodoctor.transport_logging import suppress_sensitive_http_transport_logs


async def async_main() -> None:
    settings = Settings.load()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [autodoctor] %(message)s",
    )
    suppress_sensitive_http_transport_logs()
    install_nonfatal_log_coalescing(every=500)

    store = SeedAwareIncidentStore(
        "/data/autodoctor.db",
        seed_enabled=settings.memory_seed_enabled,
        max_incidents_retained=settings.max_incidents_retained,
    )
    ha = HomeAssistantClient()
    llm = build_provider(settings)
    mcp = MCPBackend(settings)
    engine = CaseAwareAutoDoctorEngine(settings, store, ha, llm, mcp)
    executor = AutoApplyRepairExecutor(settings, store.path, ha, mcp, engine.cases)
    automatic_repairs = AutomaticRepairCoordinator(settings, engine.cases, executor)
    dashboard = ControlDashboard(settings, store, engine, executor)

    await store.initialize()
    ai_usage_recovery = await recover_orphaned_ai_usage(store.path)
    await mcp.start()
    reconciliation = await engine.initialize_case_management()
    interrupted_reopened = await recover_interrupted_case_investigations(engine)
    reconciliation["interrupted_investigations_reopened"] = interrupted_reopened
    reconciliation["ai_usage_recovery"] = ai_usage_recovery.as_dict()
    engine.backlog_reconciliation = dict(reconciliation)
    await executor.initialize()
    resumed = await executor.resume_pending_verifications()
    logging.getLogger(__name__).info(
        "Case backlog reconciliation complete: cases=%s legacy_notifications_dismissed=%s "
        "interrupted_investigations_reopened=%s; AI usage recovery legacy_unknown=%s "
        "released_pre_provider=%s retained_inflight=%s released_cost=$%.8f retained_cost=$%.8f; "
        "pending_repair_verifications_resumed=%s",
        reconciliation.get("cases", 0),
        reconciliation.get("legacy_notifications_dismissed", 0),
        interrupted_reopened,
        ai_usage_recovery.legacy_unknown,
        ai_usage_recovery.released_pre_provider,
        ai_usage_recovery.retained_inflight,
        ai_usage_recovery.released_cost_usd,
        ai_usage_recovery.retained_cost_usd,
        resumed,
    )
    await dashboard.start()

    auto_repair_task: asyncio.Task[None] | None = None
    if automatic_repairs.enabled and executor.enabled:
        logging.getLogger(__name__).warning(
            "Automatic low-risk repair is enabled; only newly-created plans that pass the existing deterministic executor gates may run"
        )
        auto_repair_task = asyncio.create_task(
            automatic_repairs.run_forever(),
            name="autodoctor-automatic-repair",
        )

    try:
        await engine.run_forever()
    finally:
        if auto_repair_task is not None:
            auto_repair_task.cancel()
            await asyncio.gather(auto_repair_task, return_exceptions=True)
        await dashboard.stop()
        await executor.close()
        await mcp.close()
        await ha.close()
        await llm.close()


if __name__ == "__main__":
    asyncio.run(async_main())
