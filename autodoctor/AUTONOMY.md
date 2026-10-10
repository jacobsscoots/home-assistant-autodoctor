# Enrolled autonomy (v0.6.0)

AutoDoctor can handle known, explicitly enrolled faults without an AI request or a
human approval click. Unsupported faults and uncertain repair outcomes still need
review. This is not permission for generic YAML editing or arbitrary service calls.
All new enrollment and external-history options default to empty/off. Existing
settings and evidence remain intact. No live target is enrolled by this release.

## 1. Verify the live installation first

Install v0.6.0 manually after the PR test/container checks and applicable quality
gate pass. Keep app auto-update off. Confirm the version in the ingress dashboard,
existing incident/AI history, backup password setup, and current recovery holds.
Save the repair-backup password outside HA. Keep normal whole-system/off-device
backups; AutoDoctor's snapshots do not include its own incident database.

Check that `system_log.fire_event` is enabled. A quiet error stream is not proof that
everything is healthy. The new health probes use native reads independently of that
stream and do not pretend their observations are system-log events.

## 2. Enable the app watchdog

Enable Home Assistant's **Watchdog** switch for AutoDoctor and **Start on boot**.
The manifest supplies `http://[HOST]:8099/live`. This route returns only
`{"alive": true}` or `{"alive": false}`, with HTTP 200/503. It is GET-only and
accessible only through ingress or Supervisor's internal app network. Full health,
configuration, evidence and approval routes remain ingress-only. It never exposes
targets, counters, worker names or credentials to the probe.

Workers that unexpectedly crash or return restart locally with bounded backoff.
Five consecutive short-lived failures stop the worker and fail liveness. Worker
heartbeats detect stalled periodic work. The log watcher is allowed to remain idle
without error events; lack of log events alone never triggers a restart. Individual
event-processing calls have a 180-second deadline.

Native HA WebSocket disconnects reconnect with 2–60 second backoff, including clean
server closes. The health API reports real watcher connectivity separately from
process liveness. AI/GitHub/MCP downtime and an exhausted AI budget do not cause
restart loops: those dependencies can be unavailable while local workers remain
alive. A process crash during a repair keeps the existing durable no-replay holds.

## 3. Enroll selected read-only health checks

| Option | Default | Meaning |
| --- | --- | --- |
| `proactive_checks_enabled` | false | Enable native read-only entity/integration checks |
| `proactive_entities` | [] | Exact live entity IDs to watch for missing/unknown/unavailable states |
| `proactive_stale_entities` | [] | Exact regularly reporting entity IDs to additionally watch for stale reports |
| `proactive_integration_entries` | [] | Exact config-entry IDs to observe; no repair enrollment implied |
| `proactive_check_interval_seconds` | 60 | Poll interval, 30–3600 seconds |
| `proactive_unavailable_grace_seconds` | 180 | Sustained unhealthy observation window before opening a case |
| `proactive_stale_seconds` | 900 | Report-age threshold for stale-check targets only |

Up to 20 unique valid entity targets and 20 integration targets are checked per
cycle; additional targets are not checked. Missing entities, disabled entries and
unsupported failure states are observation-only. Failed HTTP/authentication reads
are not evidence of target failure or recovery. A read failure breaks a target's
confirmation streak. Two separated successful observations and the grace window
are required before recording a fault. Confirmation timing persists in SQLite;
large sampling gaps reset it, and duplicate scans cannot manufacture occurrences.

Staleness uses `last_reported`, falling back to `last_updated`. Only enroll sensors
that are expected to report regularly. A light remaining off for hours is not a
stale sensor. This can observe phone-reporting failures without changing presence
or Home/Away logic. HA cannot make a sleeping/offline phone resume reporting.

Health observations get separate per-target cases and do not feed external AI.
Their exact enrolled identity is available locally; GitHub exports only structured
safe fields. Two healthy observations can retire an observation-only case, but
never erase an active repair or recovery hold. Observed recovery is not recorded
as an AutoDoctor-executed repair.

## 4. Enroll the deterministic integration-reload recipe

| Option | Default | Meaning |
| --- | --- | --- |
| `integration_reload_repair_enabled` | false | Enable the compiled reload planner and its native integration probes |
| `integration_reload_targets` | [] | Exact owner-reviewed live config-entry IDs eligible for automatic reload |

This also runs with `ai_provider: none`, MCP offline, or an exhausted AI budget.
Both executor/auto-apply switches and confirmed-backup setup are still required
for automatic execution. Without auto-apply, the compiled plan can be reviewed
through the existing ingress approval path.

The fixed recipe supports only `tplink`, `hue`, `lifx` and `wled`, and only entries
that are enabled and repeatedly in `setup_retry` or `not_loaded`. `setup_error`,
authentication/configuration failures, already-loaded entries and unsupported
domains do not qualify. Only enroll non-critical entries whose reload effects have
been reviewed. Do not enroll an entry that also controls critical equipment.
The 1.0 plan confidence describes deterministic eligibility, not certainty about
the underlying cause or a promise that a reload will fix it.

Identity, enrollment, domain and state are rechecked before and after backup.
The existing single-flight journal, cooldown, encrypted backup confirmation,
verification and recovery holds remain mandatory. Verification checks native
loaded status and absence of case recurrence. It does not prove every physical
device or automation has worked.

**Upgrade change:** all automatic integration reloads, including AI-proposed ones,
now require this exact enrollment and supported state/domain. Old automatic reload
settings alone no longer authorize a reload. The existing manually approved reload
path remains subject to its prior validator and backup gates. Old pending plans are
never silently executed when settings change or the app restarts.

Presence, sleep, climate, shutdown/power-cut, locks/security, credentials, Recorder
and arbitrary control-automation changes remain outside unattended repair.

## 5. Verification recovery

Evidence-only reconciliation runs at startup and every 60 seconds for all three
existing repair types. This never recreates a backup or replays a reload/config
write. It requires the protected owned backup, a certain completed mutation,
unchanged case evidence, and a positively verified live result. Configuration
recipes also require the exact postimage and a qualifying natural trace.

`repair_reconciliation_max_age_seconds` defaults to 86400 (24 hours). Older,
uncertain, recurring, conflicting or replaced repairs stay held for review.
Temporary evidence-access failures and late natural runs can recover within that
window. The original mutation timestamp is retained. Reconciliation checks at most
20 attempts per scan and never converts silence alone into a success claim.

## 6. Optional GitHub history

| Option | Default | Meaning |
| --- | --- | --- |
| `github_history_enabled` | false | Enable an independent, durable history outbox |
| `github_history_repository` | empty | Exact `owner/repository` for the mirror |
| `github_history_token` | empty | Dedicated fine-grained GitHub token, stored as a password option |

Use a token restricted to that one repository: **Issues read/write**, **Metadata
read** only. No Contents/Actions/Administration write or secrets access. Do not
reuse a general-purpose token or put it in source, logs or a public issue.

This implementation exports only fixed structured fields: fingerprint, UTC event
timestamps, counts, failure class, lifecycle status, version and verified repair
provenance. Raw symptoms, logger/family names, AI prose, private identities, network
values and model prompts stay local. This deliberately narrows the original #17
proposal to avoid relying on free-text redaction for public publishing.

One issue per operational case is identified by a SHA-256 case marker. Recurrences
update that issue and reopen it. Related fingerprints share a case; they do not
create separate issues for every occurrence. Existing manual history is not
rewritten as an AutoDoctor repair. The mirror does not adopt unmarked manual issues.

The SQLite outbox aggregates updates and survives restarts. At most one issue is
updated per minute. Before creation, AutoDoctor checks a bounded complete inventory
of up to 500 repository issues/PRs across open and closed states; an incomplete or
ambiguous search blocks creation. Repository changes have independent mappings.
Mapped issues are checked for the ownership marker before their body is updated;
use issue comments for human notes. GitHub failures have 60–3600 second backoff
and cannot stop local incident capture.

A create request with a lost/uncertain response is **never blindly repeated**.
AutoDoctor searches for its marker and adopts the created issue if found. If none
can be found, the durable pending-create hold remains visible for review. A
definitive rejected request can retry after its credential/rate-limit problem is
fixed. Do not clear an uncertain hold without checking GitHub first.

Only a backed-up, executed and positively verified repair can close an issue as
AutoDoctor-repaired. Manual resolution, quiet retirement and natural recovery
remain explicitly unverified and leave the external issue open. GitHub is a view;
`/data/autodoctor.db` remains authoritative.

## Validation before relying on unattended operation

Tests use temporary databases, fake HA/Supervisor/GitHub transports and synthetic
identities only. Passing tests is not a live backup/restore or repair rehearsal.
Verify installed settings and watch normal incidents, a normal app restart, HA/MCP
reconnection and later evidence. Check that holds remain held, no write is replayed,
and unsupported faults have one clear local case. No Codespace, tunnel, extra
database, embedding service or additional runtime dependency is required.
