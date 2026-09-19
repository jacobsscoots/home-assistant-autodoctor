from __future__ import annotations

import asyncio

from test_backup_first_repairs import make_plan, stack, verify
from autodoctor.case_lifecycle import LifecycleIncidentCaseManager


async def use_lifecycle(executor, ha):
    notices = {}
    dismissed = []

    async def notify(title, message, notification_id):
        notices[notification_id] = (title, message)

    async def dismiss(notification_id):
        dismissed.append(notification_id)
        notices.pop(notification_id, None)

    ha.notify = notify
    ha.dismiss_notification = dismiss
    manager = LifecycleIncidentCaseManager(executor.db_path, ha)
    await manager.initialize()
    executor.cases = manager
    return manager, notices, dismissed


def test_verified_result_immediately_dismisses_owned_case_notice(tmp_path):
    async def run():
        async with stack(tmp_path) as (_, store, _, ex, ha, _, _, clock):
            cases, notices, dismissed = await use_lifecycle(ex, ha)
            plan = await make_plan(store, cases, clock)
            result = await ex.auto_execute(plan["plan_id"])
            case = await cases.get_case(plan["pattern_key"])
            nid = case["notification_id"]
            assert nid in notices
            await verify(ex, result, clock)
            assert nid not in notices
            assert dismissed == [nid]
            assert (await cases.get_case(plan["pattern_key"]))["last_notification_at"] is None
    asyncio.run(run())


def test_recovered_repair_hold_is_republished_without_waiting_for_incidents(tmp_path):
    async def run():
        async with stack(tmp_path) as (_, store, _, ex, ha, _, _, clock):
            cases, notices, _ = await use_lifecycle(ex, ha)
            plan = await make_plan(store, cases, clock)
            attempt = await ex.journal.claim(plan, "entry_test123", "automatic", clock[0], {}, {})
            await ex.journal.advance(attempt["execution_id"], "backup_requested")
            case = await cases.get_case(plan["pattern_key"])
            nid = case["notification_id"]
            await cases.publish_case(plan["pattern_key"], force=True)
            previous = notices[nid]
            assert await ex.resume_pending_verifications() == 0
            assert notices[nid] != previous
            assert (await cases.get_case(plan["pattern_key"]))["status"] == "needs_user_action"
            assert (await ex.journal.health())["uncertain_attempts"] == 1
    asyncio.run(run())
