# AutoDoctor configuration

## Required Home Assistant setting

AutoDoctor's primary detector needs Home Assistant to fire log events:

```yaml
system_log:
  fire_event: true
```

The current target configuration supplied for verification contains this setting, so no YAML
change is required for initial monitoring.

## Live-data rule

The live Home Assistant instance is authoritative. Repository copies of Home Assistant YAML may
be snapshots and may be out of date.

AutoDoctor must therefore never use repository `automations.yaml`, `scripts.yaml`, helpers,
entity inventories, or `configuration.yaml` to infer a current repair target. v0.1 does not read
those files at runtime. Future repair execution must resolve the current target from live HA/MCP
data immediately before reading or changing configuration and must stop if resolution is
ambiguous.

## Recommended first-run settings

Keep `ai_provider: none`, `mcp_enabled: false`, and `auto_apply_low_risk: false` for the first
run. Confirm the ingress dashboard is receiving real errors without flooding or obvious false
fingerprints. Then enable one AI provider.

### OpenAI

Set `ai_provider: openai`, provide an OpenAI API key, and enter the exact model ID in `ai_model`.
AutoDoctor intentionally does not choose a model implicitly.

### Anthropic

Set `ai_provider: anthropic`, provide an Anthropic API key, and enter the exact model ID in
`ai_model`. AutoDoctor intentionally does not choose a model implicitly.

## MCP integration

MCP is optional in v0.1. AutoDoctor uses it only to verify connectivity and discover available
tools. It is intended to use `ganhammar/hass-mcp-server` at `https://YOUR_HA/api/mcp` (or the
local HTTP equivalent) with native Long-Lived Access Token authentication enabled.

v0.1 deliberately does not request automation/script configuration: Home Assistant entity IDs
and the upstream MCP configuration identifiers are different shapes, so AutoDoctor will not
guess. Explicit identifier resolution and read-only config enrichment are a v0.2 gate.

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
