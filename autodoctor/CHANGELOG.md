# Changelog

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
