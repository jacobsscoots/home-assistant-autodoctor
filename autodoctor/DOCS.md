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
SQLite incident storage, and notifications continue normally.

The health endpoint/dashboard exposes the current UTC month, estimated spend, stop threshold,
remaining allowance, analyses, failed/reserved calls, blocked calls, and token counts.

Existing `max_ai_analyses_per_hour`, `analysis_cooldown_seconds`, and
`min_occurrences_for_ai` controls remain independent secondary brakes. Failed and budget-blocked
attempts also start the per-incident cooldown so a repeating HA error cannot create a tight retry
loop.

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
entity IDs are replaced with per-prompt aliases such as `sensor.entity_1`, and `friendly_name` is
not included in the external context.

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
