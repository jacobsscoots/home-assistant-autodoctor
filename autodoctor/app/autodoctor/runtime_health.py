"""Local worker supervision. External dependency failures never request a restart."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

_LOG = logging.getLogger(__name__)


class WorkerSupervisor:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.workers: dict[str, dict[str, Any]] = {}
        self.closing = False

    def start(self, name: str, factory: Callable[[], Awaitable[None]], *, max_silence: float = 0) -> asyncio.Task:
        existing = self.workers.get(name)
        if existing and not existing["task"].done():
            return existing["task"]
        record = {"state": "starting", "restarts": 0, "heartbeat": self.clock(),
                  "max_silence": max_silence}
        self.workers[name] = record
        record["task"] = asyncio.create_task(self._guard(name, factory, record), name="autodoctor-" + name)
        return record["task"]

    def beat(self, name: str) -> None:
        if name in self.workers:
            self.workers[name]["heartbeat"] = self.clock()

    async def _guard(self, name: str, factory: Callable, record: dict) -> None:
        failures = 0
        while not self.closing:
            began = self.clock()
            record.update(state="running", heartbeat=began)
            try:
                await factory()
                if self.closing:
                    return
                raise RuntimeError("worker unexpectedly returned")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log exception details: clients may embed credentials/config.
                failures = 1 if self.clock() - began >= 60 else failures + 1
                record["restarts"] += 1
                record["state"] = "retrying" if failures < 5 else "failed"
                _LOG.warning("Worker %s interrupted; local recovery attempt %d", name, failures)
                if failures >= 5:
                    return  # Supervisor watchdog may recover the process; durable writes are not replayed.
                await asyncio.sleep(min(60, 2 ** failures))

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        workers = {}
        for name, record in self.workers.items():
            age = max(0, now - record["heartbeat"])
            state = record["state"]
            if record["max_silence"] and age > record["max_silence"] and state == "running":
                state = "stalled"
            workers[name] = {"state": state, "restarts": record["restarts"],
                             "heartbeat_age_seconds": round(age, 1)}
        healthy = bool(workers) and not self.closing and all(
            item["state"] in {"starting", "running", "retrying"} for item in workers.values()
        )
        return {"alive": healthy, "workers": workers}

    async def stop(self, name: str) -> None:
        record = self.workers.pop(name, None)
        if record:
            record["task"].cancel()
            await asyncio.gather(record["task"], return_exceptions=True)

    async def close(self) -> None:
        self.closing = True
        for name in list(self.workers):
            await self.stop(name)
