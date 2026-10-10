# Changelog

## 0.6.0

- Add Supervisor liveness probing, bounded worker recovery, actual watcher health and
  backoff after clean WebSocket closes; retain ingress-only diagnostics/approval.
- Add opt-in bounded native entity/staleness/integration observations with persistent
  confirmation timing and no invented system-log events or external AI calls.
- Add the owner-enrolled AI-independent integration reload recipe; require exact
  enrollment for all automatic integration reloads, preserving backup/verification gates.
- Extend evidence-only late verification to integration and diagnostic repairs; retain
  uncertain, conflicting, recurring, superseded and expired recovery holds.
- Add opt-in structured-only GitHub history with a durable outbox, bounded aggregation,
  repository-scoped mappings and no blind retry after an uncertain issue creation.
- Correct dashboard automatic-mode copy and show worker/check/history telemetry.
- Preserve event timestamps while recording case mutation time for history consumers.
- New feature targets and GitHub history remain off/empty by default. Live installation,
  enrollment and repair/backup qualification are separate required deployment steps.

## 0.5.3

- Retry inconclusive audit-log verification every 60 seconds while running, including after transient evidence-access failures.
- Keep the existing backup, exact-config, no-recurrence and natural-trace checks; never replay a repair or clear an unverified hold.
- Cancel the retry worker during shutdown and omit raw exception details from retry logs.

## 0.5.2

- Prefer newest script traces during natural repair verification.
- Reconcile protected inconclusive audit-log repairs from later natural evidence without replaying mutation.
- Add lifecycle logs for backup, mutation, verification success/inconclusive state and final failure status.
- Accept the reviewed `queued` / `max: 100` audit-log execution contract alongside the legacy `parallel` / `max: 10` contract.
- Mark audit-log `last_result` as planner-scoped health state.

## 0.4.16

- Fix in-process SQLite contention with shared per-database coordination and deterministic
  connection cleanup. Preserve bounded external-lock failures and never replay repair actions.
- Remove the stale per-analysis warning claiming the v0.1 executor is disabled.
- Separate template-error cases by originating automation/script, including numeric names.
- Preserve existing mixed history unchanged; new template events start scoped cases instead of
  inheriting old diagnoses or repair approvals. Ambiguous historical records are not retroactively split.
- Add regression tests for concurrency, cancellation, rollback, read-only access, grouping,
  upgrade history, warning behaviour and duplicate-repair prevention.

No changes to options, repair permissions, allowlists, AI budget gates or verification duration.
