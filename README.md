# Home Assistant AutoDoctor App Repository

This repository is a Home Assistant App (add-on) store repository containing **AutoDoctor**.

AutoDoctor watches Home Assistant's `system_log_event` stream, fingerprints and deduplicates
incidents, stores them persistently, groups them into operational cases, and can optionally request
conservative read-only AI diagnoses. Its diagnostic MCP boundary is allowlisted/read-only and
fail-closed. Automatic repairs remain disabled: when the repair executor is enabled, it can execute
only the tiny deterministic repair allowlist after an individual Home Assistant ingress approval.

## v0.4.10 lifecycle and dashboard

v0.4.10 tightens the distinction between evidence, active problems, and resolved history:

- proven non-fatal raw `python-kasa` sub-call logs emitted during TP-Link configured-entry polling
  remain in the evidence ledger but skip AI analysis, persistent notifications, and repair planning;
- genuine Home Assistant TP-Link coordinator failures remain fully actionable and are not covered by
  that narrow observation-only rule;
- persistent notifications are owned by active AutoDoctor cases only. Resolved, historical, and
  observation-only cases have their AutoDoctor notification dismissed;
- ordinary quiet `new`, `diagnosed`, or `reopened` cases retire to historical after 24 hours without
  recurrence, while `needs_user_action`, `repair_available`, `investigating`, and `verifying` are never
  auto-retired;
- a future recurrence reopens resolved/historical cases through the existing incident lifecycle;
- the ingress dashboard is responsive, dependency-free, redacts private network values in displayed
  incident evidence, and exposes repair approval separately from a safe local "mark resolved & dismiss"
  case action.

The manual resolve action changes only AutoDoctor's local case lifecycle and its own notification. It
never calls a Home Assistant repair service, and it cannot hide a case that is actively investigating,
awaiting repair approval, or verifying a repair.

## Local AutoDoctor memory

AutoDoctor keeps its long-term operational memory in `/data/autodoctor.db`. Before an AI diagnosis,
it can retrieve a small bounded set of relevant historical knowledge and observed topology locally.
The memory system includes:

- verified manual fixes and AI hypotheses stored separately from raw incidents;
- trust classes/scores so verified fixes outrank unverified AI guesses;
- expiry/freshness metadata and Home Assistant/AutoDoctor version provenance;
- persistent random entity pseudonyms, so the same real entity has a stable private alias across incidents/restarts;
- broad failure-pattern signatures above exact fingerprints;
- an evidence-only topology graph built from relationships actually observed in incidents;
- outcome feedback when a diagnosed incident continues, recurs heavily, or remains quiet;
- SQLite FTS5 retrieval with a local fallback when FTS5 is unavailable.

Historical memory is supplied to the AI as evidence, never as authority. AutoDoctor explicitly warns
the model not to copy an old fix blindly.

## Home Assistant incident & repair history

The repository's **Issues** tab is also used as a durable, sanitised history of Home Assistant
problems, diagnoses, fixes and verification results.

- [Master incident & repair history](https://github.com/jacobsscoots/home-assistant-autodoctor/issues/16)
- [Planned automatic GitHub Issues mirroring](https://github.com/jacobsscoots/home-assistant-autodoctor/issues/17)

Historical manual repairs are explicitly identified as manual. An issue must never claim
"fixed by AutoDoctor" unless AutoDoctor actually executed the repair and the result was verified.

A reusable incident template is available at
`.github/ISSUE_TEMPLATE/home-assistant-incident.md`.

See `autodoctor/README.md` and `autodoctor/DOCS.md` for details.

To add this repository to Home Assistant: Settings → Add-ons → Add-on Store → ⋮ → Repositories →
add `https://github.com/jacobsscoots/home-assistant-autodoctor`.
