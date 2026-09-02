# AutoDoctor

AutoDoctor is an AI-assisted reliability app for Home Assistant. It watches the native
`system_log_event` stream, fingerprints recurring errors, stores incident history, gathers
bounded live context, retrieves relevant local historical memory, and can ask an explicitly
configured AI provider for a conservative read-only diagnosis.

## Source of truth

AutoDoctor treats the **live Home Assistant instance** as its source of truth. Repository copies
of `automations.yaml`, `scripts.yaml`, `configuration.yaml`, helpers, or entity inventories may be
stale and must not be used to infer current repair targets.

v0.2.0 still does not mount `/config`. With MCP disabled it uses live events/entity states exactly
as before. With MCP enabled it may add a small bounded snapshot from explicit read-only MCP tools.

## Safety boundary

**v0.2.0 cannot intentionally modify Home Assistant.** The repair executor remains hard-disabled
even if the `auto_apply_low_risk` option is enabled.

Current pipeline:

`system_log_event -> exact fingerprint + broad pattern -> dedupe -> local memory/topology retrieval -> bounded/redacted live context -> optional cached read-only MCP context -> optional AI diagnosis -> memory/outcome update -> incident record -> dashboard`

The model never receives a generic MCP tool interface. AutoDoctor's MCP client applies a compiled
read allowlist and rejects unknown or write-capable tools locally before a tool request is sent.

## Local memory / RAG

v0.1.5 adds durable local memory in `/data/autodoctor.db`. This is explicit retrieval-augmented
context: OpenAI/Anthropic do not receive a giant persistent conversation and do not remember the
house between calls on their own.

Before a diagnosis, AutoDoctor retrieves at most the configured number/character budget of useful
historical records. Retrieval is local SQLite FTS5 plus exact pattern/family ranking; if the
platform SQLite build lacks FTS5, AutoDoctor falls back to local lexical matching instead of
breaking monitoring.

### Trust model

Memory records carry a provenance/trust class:

- `observed`
- `ai_hypothesis`
- `manually_verified`
- `verified_fix`
- `failed_fix`
- `deprecated`

Verified fixes rank above AI hypotheses. Expired, superseded and deprecated memories are excluded
from normal retrieval. Every AI prompt states that memory is historical evidence, not current fact.

### Freshness and versions

Knowledge can store:

- Home Assistant version
- AutoDoctor version
- integration/logger family
- created/verified/last-confirmed timestamps
- expiry time
- `superseded_by`

AI hypotheses expire by default after 30 days. Verified historical fixes have longer local expiry
windows. Expiry prevents old advice from silently becoming permanent truth.

### Stable private aliases

Real entity IDs remain local. AutoDoctor creates a persistent random alias for each referenced
entity (for example `sensor.entity_a1b2c3d4`) and stores that mapping only in the local SQLite DB.
The same real entity keeps the same pseudonym across incidents and restarts, allowing useful
cross-incident recognition without exposing the real ID externally.

`friendly_name` remains excluded from external AI context.

### Observed topology

AutoDoctor builds a local evidence-only graph from entities that actually appear together in
incidents. When an `automation`, `script` or `scene` is observed with other entities, the graph can
record a `references_in_incident` edge. Otherwise, entities are only marked as co-occurring.
Helpers and integration/logger family nodes are retained as local topology metadata.

This is deliberately **not** presented as a complete Home Assistant configuration graph. It cannot
claim relationships that have not been observed.

### Failure patterns

Exact fingerprints still deduplicate the same concrete incident. v0.1.5 additionally creates a
broader pattern key so changing details can be grouped into one failure class — for example Kasa
query timeouts with different addresses/module lists map to the same local `device_query_timeout`
pattern.

v0.1.7 hardens exact fingerprints against volatile Kasa KLAP session IDs and signed incidental
numbers, and adds a pattern-level AI cooldown so a fresh exact fingerprint cannot trigger another
AI diagnosis of the same broader failure pattern inside the configured cooldown window.

### Outcome feedback

When an AI hypothesis already exists and the same fingerprint recurs, local memory records that the
incident `continued`. Heavy post-diagnosis recurrence can promote that outcome to `worsened` by
recurrence volume. If no recurrence is observed for the configured quiet window, memory may record
`quiet`; the AI is explicitly told that quiet is **not proof of a fix**.

Historical verified manual fixes from the repository incident ledger are seeded locally as trusted
knowledge. They remain labelled `manual-pre-autodoctor`; AutoDoctor does not rewrite them as its own repairs.

## AI budget guard

v0.1.2 added fail-closed monthly AI accounting. An external provider cannot start unless the
budget guard, a monthly budget, a lower internal stop threshold, exact model pricing, an exact
model ID, and the provider API key are all configured.

AutoDoctor reserves a conservative estimated cost **before** each AI request. If the reservation
would cross the internal monthly stop, the request is blocked while local monitoring continues.
Failed/aborted requests retain the reservation when exact usage is unavailable. Successful calls
use provider-reported token counts when available. Usage is stored in `/data/autodoctor.db` and
resets by UTC calendar month without deleting history.

Model prices are never silently hard-coded: input/output prices must be entered explicitly for the
configured model.

## AI scheduling fairness

v0.1.3 adds two independent protections against noisy integrations consuming all AI capacity:

- a per-family hourly cap groups related logger children (for example `kasa.smart.*`) and limits
  how many attempts that family can consume inside the global rolling-hour allowance;
- a startup backlog grace window keeps old incidents recording/deduplicating locally after an app
  restart while prioritising incidents first seen after the current process started.

The global hourly limit, exact-incident cooldown, broader pattern cooldown, family cap, startup
backlog guard, and monthly budget are independent brakes. None of them stop local monitoring or
incident persistence.

Safe accounting logs include estimated/resolved token and cost data without logging prompts, API
keys, authorisation headers, or raw outbound context.

See `DOCS.md` for configuration details.

## Privacy boundary

External AI context is bounded and redacted. Referenced entity IDs are replaced with stable private
pseudonyms before they leave Home Assistant, and `friendly_name` is excluded from external AI context.

MCP result payloads receive an additional recursive sanitization/bounding pass before entering AI
context. Known secret fields, IP addresses, email addresses, entity IDs and location coordinates
are removed or replaced.

## Feedback-loop protection

AutoDoctor deliberately ignores log events whose logger name or message contains `autodoctor`.
This prevents its own warnings from becoming new incidents. Use neutral wording for synthetic
smoke tests.

## AI providers

- `none`: monitor/deduplicate/memory only, no external AI calls.
- `openai`: direct OpenAI API; exact model/prices/budget must be configured.
- `anthropic`: direct Anthropic API; exact model/prices/budget must be configured.

API usage is billed separately from consumer ChatGPT/Claude subscriptions.

## MCP

v0.2.0 adds optional **read-only** Home Assistant MCP enrichment using the MCP Python SDK v2
Streamable HTTP transport. The SDK intentionally uses `httpx2`, not `httpx`, and AutoDoctor pins
both dependencies in its container build.

When enabled, AutoDoctor automatically refreshes only two small diagnostic reads every five
minutes: `get_system_status` and `list_integrations`. Results are cached, bounded and redacted
before they are added to a diagnosis. `get_config` is intentionally excluded from automatic AI
context because upstream responses may include Home Assistant location metadata. The AI cannot
request MCP tools itself.

Every AutoDoctor tool attempt is recorded without arguments/results in the rotating local
`/data/mcp_audit.log`, including locally rejected attempts. Unknown tools and all non-allowlisted
tools fail closed before network tool execution.

Use a dedicated Home Assistant user/token with the least permissions compatible with the required
reads. A token may technically retain permissions broader than AutoDoctor's local allowlist, so
credential least privilege remains defence in depth rather than a substitute for the application
boundary.

See `MCP_READ_ONLY.md` for the allowlist, audit format, setup and acceptance checks.
