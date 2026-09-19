# Backup-first repairs (v0.5.0)

## Scope and one-time setup

All **new** automatic and manually approved repairs use `BackupFirstRepairExecutor`.
The existing integration-reload gates remain. A separate compiled diagnostic recipe
is opt-in; its enrolled target list is empty by default. No live target was inspected,
enrolled or changed during development. This release does not assert that it fixes
any particular deployment's previously reported template errors.

Before new repairs can run, set `repair_backup_password` to at least 12 characters.
Save it outside Home Assistant; retain previous passwords while their snapshots remain.
It is never printed, exposed by the health API or passed to the AI. Missing backup setup
blocks repairs, not incident monitoring. Keep add-on auto-update OFF.

| Option | Default | Meaning |
| --- | --- | --- |
| `repair_backup_password` | empty | Required backup encryption password |
| `repair_backup_keep` | 2 | One or two owned backups per stable target and repair type |
| `repair_backup_max_size_mb` | 256 | Maximum accepted size of one backup, in MiB |
| `repair_backup_max_total_mb` | 2048 | Total recorded owned-backup budget, in MiB |
| `repair_backup_min_free_mb` | 512 | Free-space reserve beyond one maximum-sized snapshot |
| `diagnostic_template_repair_enabled` | false | Explicit opt-in for the compiled recipe |
| `diagnostic_repair_entities` | [] | Exact owner-enrolled live `automation.*` entity IDs |

The add-on now requests Supervisor's **backup** role, not manager/admin, plus its
existing Core API access. That platform role permits backup operations broadly;
AutoDoctor's separate deterministic client restricts actual calls to create, inspect
and delete its recorded local snapshots. No generic model write tool, shell executor,
Docker access, configuration mount or automatic full-system restore was added.
The diagnostic MCP remains read-only.

## Execution and retention

1. Validate the plan, permissions, exact live target and preconditions.
2. Persist a global single-flight claim and private target recovery record.
3. Check space/budget; create an encrypted partial HA configuration backup, excluding
   Recorder history, unrelated add-ons and media.
4. Confirm the returned job and every child job completed without errors, then check
   the snapshot's slug, ownership, encryption, contents and size.
5. Recheck the live target after the backup and recheck snapshot existence before the
   one allowed mutation. No backup request or HA mutation is automatically replayed.
6. Wait the configured verification period and check real read-only evidence plus
   incident recurrence before recording success.
7. Only after success, prune eligible older snapshots for the same target and type.

Deletion requires the locally recorded backup ID AND matching installation owner,
execution ID, stable target key and random nonce in Supervisor metadata. A similar
name is not ownership. Ordinary/manual backups, other installations and remote copies
are not deleted. Retention spans attempts at the same fix, not a new quota per analysis.
A temporary third snapshot is allowed while replacing two good recovery points.

Failed, inconclusive or interrupted repairs pin their recovery point and block further
attempts for that target. Uncertain backup/mutation outcomes block all new repairs
pending review. These holds survive restart and have no automatic override. Restart
resumes recorded verification only, never backup creation or mutation. A per-target/type
one-hour cooldown and a durable global execution claim prevent repair loops.

Cleanup failure does not turn a verified repair into failure. It reports attention
needed and stops further deletions. Missing/mismatched metadata, protected snapshots
or exhausted budgets never cause wider deletion. Associated private target preimages
are cleared when their eligible snapshot is deleted.

The Supervisor API has no streaming backup size cap: actual size is checked after
creation, so an unexpectedly large archive can exceed the acceptance limit. The free
space check and reserve reduce risk but cannot guarantee space against other writers.
Completed-job and metadata checks are not a restore rehearsal or cryptographic archive
integrity proof. Keep normal whole-system and off-device backups too.
Supervisor `protected=true` means **encrypted**, not retention-pinned; AutoDoctor uses
its own journal flag to protect recovery points after failures.

## Compiled diagnostic recipe

`diagnostic_trigger_state_v1` guards direct `{{ trigger.from_state.state }}` and
`{{ trigger.to_state.state }}` interpolation in log messages. Missing trigger/state
values become `unknown`; present values retain their value. The fixed transformation
never changes triggers, conditions or control actions and never accepts AI-authored YAML.

It requires recent repeated real matching error evidence, the exact enrolled automation
logger, stable Core **2026.9.3 or newer**, a live identity/configuration match and an
action list consisting entirely of direct `system_log.write` or `logbook.log` calls.
It refuses physical services, indirect scripts, choose/control blocks, response variables
and unknown action shapes. Display names and repository snapshots do not establish
current identity or eligibility. Review the actual live error and configuration before
enrolling anything. Critical-control automations must not be enrolled.

This planner can create an eligible fixed plan without waiting for AI `propose_fix`.
The executor regenerates it from current configuration and checks both expected hashes.
The planner's health summary explains missing evidence, unsupported shapes or other
withholding reasons without exposing configuration.

Verification requires matching configuration readback, a subsequent completed live
trace containing the exact patched configuration, and no new error from the origin/case.
A trace summary alone is insufficient. AutoDoctor never triggers an automation to
manufacture a successful test. No qualifying run in the verification window means
**inconclusive**, not fixed; its snapshot stays protected and review is required.

A demonstrated regression attempts target-only rollback only when current configuration
still matches AutoDoctor's postimage. An observed intervening edit is not overwritten.
Target preimages are private configuration objects, not exact copies of the whole file;
the pre-repair Supervisor backup is the original-file recovery point.

### Native editor concurrency limit

HA's native config POST validates before saving and reloads the selected automation,
but it serializes the shared automation file and has no external atomic `If-Match` API.
AutoDoctor checks immediately before saving, but an independent UI/file editor could
still change it between read and write. The same race applies to conditional rollback.
**Do not edit enrolled diagnostic automations concurrently; disable the recipe first.**
No atomic cross-editor compare-and-swap or byte-for-byte file preservation is claimed.

## Integration reloads and reporting

A reload additionally requires the exact entry to be enabled and in `setup_error`,
`setup_retry` or `not_loaded`, including after backup completion. Already-loaded entries
are not reloaded merely to exercise the path. Verification requires that entry loaded
and no case recurrence. Restoring configuration cannot reverse physical side effects.

`/api/repair-executor` includes `backup_safety`; `/api/health` includes planner counters
and the concrete last reason. Passwords, private configuration and raw backup metadata
are not exported. Generic code/YAML edits and automatic full-system restore remain
outside the allowlist.

Tests use temporary databases and fake transports only. Passing tests or metadata checks
does not prove a live backup, restore or repair. Install manually only after the full CI,
container checks and post-merge Sonar gate pass, then complete backup/enrolment setup.
