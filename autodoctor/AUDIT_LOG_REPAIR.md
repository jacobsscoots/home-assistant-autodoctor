# Reviewed JSON/Base64 logger repair (v0.5.1)

This recipe addresses an intermediate-template-variable type conversion, not a missing
trigger state. Caller automations and packages are not edited. It changes only:

```yaml
# Before, in the first variables action:
json_line: '{{ payload | to_json }}'
b64: '{{ json_line | base64_encode }}'

# After (json_line removed):
b64: '{{ payload | to_json | base64_encode }}'
```

HA can parse a JSON-looking intermediate string into a native dictionary `Wrapper`.
`base64_encode` accepts strings/bytes, not that wrapper. Keeping JSON serialization
and encoding within one expression avoids the intermediate native-result boundary.
The existing payload, keys/types, script fields, terminal shell service and quoting
are unchanged. No template or shell command is evaluated by the compiler.

## One-time activation after manual update

Keep the existing backup password, retention of two and auto-update OFF. No need to
create a different password, move package automations, or enable the unrelated
`diagnostic_template_repair_enabled` option.

Set these three new options only after reviewing the **live** target:

- `audit_log_repair_entity`: the exact logger `script.*` entity (empty by default).
- `audit_log_repair_config_sha256`: the digest of the complete reviewed raw config.
- `audit_log_repair_enabled`: `true` (default `false`).

Enrollment asserts that the existing terminal shell command and receiving helper were
reviewed as append-only diagnostic logging, that native-editor rewriting of scripts.yaml
is acceptable, and that no configuration editor/file writer will run concurrently. This
assertion is human-reviewed; AutoDoctor has no new filesystem/shell access and cannot
independently inspect or hash the helper. Disable this recipe before changing that helper.
It does not let AI choose targets or author shell commands.

Read the configured entity through native `script/config`, resolve its registry
`unique_id` (the script config key; do not infer it from a renamed entity), then compare
with GET `/api/config/script/config/<key>`. Both must agree. Run the pure compiler before
enrollment and calculate the digest privately using:

```python
from autodoctor.audit_log_recipe import compile_repair
from autodoctor.repair_journal import config_digest
compile_repair(current_raw_config)  # Raises on unsupported shapes; performs no writes.
approved_digest = config_digest(current_raw_config)
```

Do not copy configuration into chat, GitHub or logs. Restart AutoDoctor once after saving
its options. Never invoke the logger/automations, seed errors or construct manual plans
to manufacture a test. Two distinct recent matching **child-script** errors after startup
can create a compiled plan without an AI `propose_fix`. Caller errors cannot pick a target.
Previously diagnosed cases can qualify; existing repair/user-action holds are not cleared.

## Strict applicability

Only the reviewed Core **2026.9.3** is accepted; other versions require requalification.
The recipe accepts either the original `parallel`/`max: 10` execution contract or the reviewed
burst-safe `queued`/`max: 100` contract, plus the eight reviewed diagnostic fields,
exactly two sequence actions, and first-step variables in order `payload`, `json_line`,
`b64`. The only terminal action is the unchanged, operator-reviewed `shell_command.*`
with exactly the existing Base64 argument. The queued contract serializes append operations and
keeps up to 100 invocations queued; it does not alter the Base64 payload expression. Templates
with extra filters, extra actions,
blueprints, arbitrary parameters or missing preconditions are rejected.

The full raw-config digest binds the payload construction and terminal service. It is
checked again before executing; shape checks alone are not authorization. Operator edits
invalidate enrollment until reviewed again. Already-correct encoding is not changed.

## Backup, concurrency and verification

This uses the existing backup-first executor, durable claims/recovery holds and one/two
owned-backup retention. There is no alternate unbacked script path, new Supervisor role,
write MCP, filesystem mount, generic service caller or automatic full restore.

The script must be idle (`off`, `current=0`) at resolution and immediately before saving.
The native script editor validates then writes its UI-managed scripts file and schedules
script reload. In Core 2026.9.3, unchanged scripts are retained; changed/removed scripts
are unloaded. Pending unsaved changes to other script definitions could therefore be
picked up by reload. No concurrent edits or pending unrelated script changes are allowed.
Saving also reserializes scripts.yaml, so comments/formatting can change elsewhere.

These checks are **not** atomic across a UI/file editor or a new script invocation. A run
can start after the idle check and be cancelled by reload, potentially losing a diagnostic
line. Operator enrollment accepts this narrow logging-only limitation; it is not suitable
for scripts that control devices or critical behaviour. A detected busy state, identity
change, config change or unsupported editor source blocks the repair.

After the configured minimum observation window (normally 120 seconds), verification
requires a natural completed run using the exact patched configuration, a JSON/Base64
roundtrip into the unchanged terminal service argument and no new origin/case error.
The payload must have the formerly-failing Python-literal-compatible shape: a natural
null/boolean-containing run that already worked before is insufficient. Only sanitized
proof flags are retained, not the payload or trace.

If that evidence has not arrived, the repair stays `verifying`, checking every 15 seconds
for up to 15 minutes (or the configured minimum if longer). It is then **inconclusive**,
not successful, and its backup remains protected. Restart resumes verification, never
replays the write. Demonstrated regression permits the existing conditional target-only
rollback; observed intervening edits and uncertain saves are not overwritten/replayed.

Without a response variable, the script trace does not retain the shell exit status.
Success therefore means **the encoding fault was exercised and resolved**, not an
independent proof that the helper appended a record. Verification records explicitly
set `log_append_verified=false`; no extra response variable/action is added to expand
the approved patch. Normal log inspection can independently establish the append outcome.

## Read-only readiness without bypassing ingress

The existing authenticated `/api/qualification` response now includes aggregate
`repair_safety`: journal availability, uncertain attempts, active executions and target
hold count. Missing journals return unknown counts rather than falsely saying no holds.
It also exposes the compiled logger planner's sanitized enrollment/result counters.
The full dashboard/health routes remain ingress-only. Do not spoof headers or weaken
access controls when those routes return 403 to a direct add-on call.

## Test evidence and limits

Tests cover the exact compiler delta, 13 payload/encoding cases, native variable-boundary
failure, enrollment/digest/identity checks, backup gating, idle/write checks, bounded
natural verification, rollback, restart/no replay and read-only aggregate hold visibility.
The self-contained encoding test models the relevant native-result/filter contracts;
it is not a full HA engine. Development also runs an isolated Jinja/JSON reproduction
where available. No real backup, script save, reload, helper invocation or HA repair is
claimed by these tests.

Reference source inspected: Home Assistant Core tag `2026.9.3`,
`helpers/template/__init__.py`, `helpers/template/extensions/base64.py`,
`helpers/script.py`, `components/script/__init__.py`, and `components/config/script.py`.
