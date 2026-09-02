from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger(__name__)


async def recover_interrupted_case_investigations(engine: Any) -> int:
    """Reopen cases left in ``investigating`` by a previous process.

    Analysis tasks are in-memory only. Therefore an ``investigating`` case found during
    startup cannot still have a live analysis behind it. Reclassifying it as ``reopened``
    lets the normal backlog worker reconsider it through the same budget, cooldown and
    family-cap gates. Incident evidence and repair-plan state are not modified.
    """
    manager = getattr(engine, "cases", None)
    if manager is None:
        return 0

    recovered = 0
    for case in await manager.list_cases(500):
        if str(case.get("status") or "") != "investigating":
            continue
        pattern_key = str(case.get("pattern_key") or "")
        if not pattern_key:
            continue
        await manager._set_status(pattern_key, "reopened")  # controlled internal lifecycle transition
        await manager.publish_case(pattern_key, force=True)
        recovered += 1

    if recovered:
        _LOG.warning(
            "Recovered %d interrupted case investigation(s) to reopened state for normal triage",
            recovered,
        )
    else:
        _LOG.info("No interrupted case investigations required startup recovery")
    return recovered
