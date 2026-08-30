# AutoDoctor configuration

## Required Home Assistant setting

AutoDoctor's primary detector needs Home Assistant to fire log events:

```yaml
system_log:
  fire_event: true
```

## Live-data rule

The live Home Assistant instance is authoritative. Repository copies of Home Assistant YAML may
be snapshots and may be out of date.

AutoDoctor must therefore never use repository `automations.yaml`, `scripts.yaml`, helpers,
entity inventories, or `configuration.yaml` to infer a current repair target. v0.1.x does not
read those files at runtime. Future repair execution must resolve the current target from live
HA/MCP data immediately before reading or changing configuration and must stop if resolution is
ambiguous.

## Recommended first-run settings

Keep `ai_provider: none`, `mcp_enabled: false`, and `auto_apply_low_risk: false` until the monitor
pipeline is proven stable on the target Home Assistant instance.

## Local memory / RAG (v0.1.5+)

`memory_enabled: true` enables local retrieval-augmented memory from `/data/autodoctor.db`.
No extra external service or embedding API is required. Memory retrieval happens before an AI
request and the selected history is inserted into the already bounded/redacted prompt.

Recommended defaults:

```yaml
memory_enabled: true
memory_max_items: 5
memory_max_chars: 6000
memory_ai_hypothesis_expiry_days: 30
memory_quiet_outcome_seconds: 86400
memory_worsened_recurrences: 10
```

### What is stored

AutoDoctor keeps separate local tables for:

- exact incidents/fingerprints;
- broader failure-pattern keys;
- AI usage/cost accounting;
- trusted knowledge/resolutions;
- persistent entity pseudonyms;
- observed topology nodes/edges;
- outcome history.

The local SQLite database remains the source of truth. External AI calls receive only the bounded,
sanitisied memory subset selected for that incident.

### Trust and provenance

Knowledge records carry a trust class and numeric weight:

- `observed`
- `ai_hypothesis`
- `manually_verified`
- `verified_fix`
- `failed_fix`
- `deprecated`

Verified fixes rank above AI hypotheses. Expired, superseded and deprecated records are excluded
from normal retrieval. The prompt explicitly says historical memory is evidence, not current fact.

The initial v0.1.5 database seeds several verified historical repair patterns from the repository's
Home Assistant incident ledger. They are tagged `manual-pre-autodoctor`; AutoDoctor does not claim
those repairs as its own work.

### Freshness / expiry

Knowledge can include Home Assistant version, AutoDoctor version, integration/logger family,
created/verified/last-confirmed timestamps, expiry and a `superseded_by` pointer. AI hypotheses
expire after the configured number of days so an old model suggestion cannot silently become
permanent truth.

### Stable entity pseudonyms

The real entity ID remains local. On first observation AutoDoctor assigns a random alias such as
`sensor.entity_a1b2c3d4` and persists the mapping in SQLite. The same entity keeps the same alias
across later incidents/restarts, allowing the model to recognise recurring involvement without
receiving the private real entity ID.

The alias mapping itself is never included in the AI prompt. `friendly_name` remains excluded.

### Observed topology

AutoDoctor builds an evidence-only topology graph from entities that actually appear together in
incidents:

- if an `automation`, `script` or `scene` is present with other entities, it may record
  `references_in_incident` edges;
- otherwise it records only co-occurrence, not a claimed configuration reference;
- helpers are classified locally;
- entities can be linked to the observed logger/integration family.

Only a small relevant topology slice is inserted into a diagnosis. This graph is intentionally
incomplete until more live evidence is observed (or a future approved read-only MCP phase enriches
it).

### Broad failure patterns

Exact fingerprints still identify/deduplicate one concrete failure signature. v0.1.5 also creates
a broader local pattern key. Recognised categories include device-query timeout, timeout,
connection-refused, rate-limit, authentication, template error, not-found, unavailable, storage
and parse errors.

For example, Kasa device-query timeouts with changing addresses/module lists can share one pattern
while retaining separate exact fingerprints when appropriate.

### Outcome feedback

After an AI hypothesis is stored:

- a subsequent recurrence records `continued`;
- sufficient post-diagnosis recurrence volume records `worsened`;
- no recurrence inside the configured quiet window can record `quiet`.

`quiet` is deliberately described to the model as **not proof of a fix**. Future verified manual or
AutoDoctor repairs can use stronger outcomes such as fixed/failed and supersede older hypotheses.

### Retrieval bounds / FTS

`memory_max_items` limits the number of knowledge records per prompt and `memory_max_chars` limits
the total memory narrative size. AutoDoctor uses local SQLite FTS5 when available and falls back to
local lexical matching if the platform SQLite build does not provide FTS5. Monitoring must never
fail just because FTS5 is unavailable.

`/api/health` exposes memory counts, trust/outcome summaries, stable alias count, topology size,
FTS availability and the number of matches from the most recent retrieval.

## AI budget guard (v0.1.2+)

External AI is fail-closed. Selecting `openai` or `anthropic` will only start when all of the
following are explicitly configured:

- `ai_budget_enabled: true`
- `ai_monthly_budget_usd` greater than zero
- `ai_monthly_stop_usd` greater than zero and lower than the monthly budget
- `ai_input_cost_per_million_usd` greater than zero
- `ai_output_cost_per_million_usd` greater than zero
- an exact `ai_model`
- the provider API key

The monthly budget value is the reference/provider-side ceiling. The lower AutoDoctor stop value
is the local hard stop. For example, use a $5 provider project cap with AutoDoctor stopping at
$4.50.

Model prices are deliberately **not baked into AutoDoctor**. Enter the current published input
and output prices for the exact configured model. This prevents a model price change from
silently invalidating the guard.

Before every provider call AutoDoctor creates a conservative SQLite reservation based on:

- one reserved input token per UTF-8 byte of the system prompt + incident prompt;
- an additional protocol-overhead allowance;
- the provider's maximum configured output tokens;
- the explicitly configured input/output prices.

If current monthly spend plus the reservation would exceed the internal stop, the call is not
made. Failed or interrupted calls retain their reservation if exact provider usage is unknown, so
retries cannot cheaply bypass the limit. Successful calls replace the reservation with the
provider-reported token usage when available. Monthly accounting uses calendar months in UTC and
persists in `/data/autodoctor.db` across restarts and updates.

Budget exhaustion only disables new AI requests. Monitoring, fingerprinting, deduplication,
SQLite incident storage, memory and notifications continue normally.

## AI scheduling fairness and backlog control (v0.1.3+)

`max_ai_analyses_per_family_per_hour` limits one logger/integration family inside the global
rolling-hour allowance. AutoDoctor derives a coarse family locally from the logger name:

- `kasa.smart.smartdevice` and `kasa.protocol` are both family `kasa`;
- `homeassistant.components.tplink.*` is family `homeassistant.components.tplink`;
- `custom_components.example.*` is family `custom_components.example`.

The effective per-family cap can never exceed the configured global hourly cap. With a global
limit of 6 and a family limit of 2, one noisy family can consume at most two attempt slots in a
rolling hour, leaving capacity for other incident families.

`ai_startup_backlog_grace_seconds` prevents a restart or provider-enable cycle from immediately
spending AI capacity on every old eligible incident. During this grace window all incidents still
record/deduplicate normally, old incidents are deferred from AI, and newly first-seen incidents can
remain eligible.

## Safe AI usage logging (v0.1.3+)

AutoDoctor logs cost/accounting metadata without logging prompts or credentials. Reservation logs
include the incident fingerprint, local family, provider/model, conservative input estimate,
maximum output reservation, reserved cost, spend before the call, and remaining internal budget.
Successful logs include provider/fallback token counts and reconciled spend.

### OpenAI

Set `ai_provider: openai`, provide an OpenAI API key, enter the exact model ID in `ai_model`, and
configure the budget/pricing fields above. AutoDoctor intentionally does not choose a model or
pricing implicitly.

### Anthropic

Set `ai_provider: anthropic`, provide an Anthropic API key, enter the exact model ID in `ai_model`,
and configure the budget/pricing fields above. AutoDoctor intentionally does not choose a model
or pricing implicitly.

## Privacy boundary for external AI

Before incident context is sent externally, AutoDoctor redacts common secret/token patterns,
email addresses, IP/MAC addresses, and long hexadecimal identifiers. Referenced Home Assistant
entity IDs are replaced with stable random pseudonyms stored only in local SQLite, and
`friendly_name` is not included in external context.

## MCP integration

MCP remains optional and should stay disabled during read-only AI diagnosis testing. v0.1.x uses
it only for connectivity/capability discovery and deliberately does not request automation/script
configuration because Home Assistant entity IDs and upstream MCP configuration identifiers are
different shapes.

## Planned safe executor gate

A future repair can only move from proposal to application if all gates pass:

1. Evidence is sufficient and the live target is unambiguous.
2. Risk policy classifies the repair as eligible.
3. MCP backup succeeds.
4. The smallest patch is applied.
5. Home Assistant configuration validation succeeds.
6. The original fingerprint is observed for a verification window.
7. Any validation/regression failure invokes MCP restore.

Presence, sleep, climate, power shutdown, locks/security, credentials, database changes,
deletions, and broad automation behaviour remain approval-required even after auto-apply is
introduced.
