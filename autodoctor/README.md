# AutoDoctor

AutoDoctor is an AI-assisted reliability app for Home Assistant. It watches the native
`system_log_event` stream, fingerprints recurring errors, stores incident history, gathers
bounded live context, and can ask an explicitly configured AI provider for a conservative
read-only diagnosis.

## Source of truth

AutoDoctor treats the **live Home Assistant instance** as its source of truth. Repository copies
of `automations.yaml`, `scripts.yaml`, `configuration.yaml`, helpers, or entity inventories may be
stale and must not be used to infer current repair targets.

v0.1.x reads live events and live entity states only. It does not mount `/config` and does not
read Home Assistant YAML snapshots at runtime.

## Safety boundary

**v0.1.x cannot modify Home Assistant.** The repair executor remains hard-disabled even if the
`auto_apply_low_risk` option is enabled.

Current pipeline:

`system_log_event -> dedupe -> bounded/redacted context -> optional AI diagnosis -> incident record -> dashboard`

MCP remains optional and does not provide repair execution in v0.1.x.

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

The global hourly limit, per-incident cooldown, family cap, startup backlog guard, and monthly
budget are independent brakes. None of them stop local monitoring or incident persistence.

v0.1.3 also logs safe AI accounting metadata after reservations and successful/failed calls:
estimated input tokens, maximum output reservation, provider-returned token counts when available,
reserved cost, reconciled cost, monthly spend, and remaining internal budget. Prompts, API keys,
authorisation headers, and raw outbound context are not logged by these accounting messages.

See `DOCS.md` for configuration details.

## Privacy boundary

External AI context is bounded and redacted. Referenced entity IDs are pseudonymised before they
leave Home Assistant, and `friendly_name` is excluded from external AI context.

## Feedback-loop protection

AutoDoctor deliberately ignores log events whose logger name or message contains `autodoctor`.
This prevents its own warnings from becoming new incidents. Use neutral wording for synthetic
smoke tests.

## AI providers

- `none`: monitor/deduplicate only, no external AI calls.
- `openai`: direct OpenAI API; exact model/prices/budget must be configured.
- `anthropic`: direct Anthropic API; exact model/prices/budget must be configured.

API usage is billed separately from consumer ChatGPT/Claude subscriptions.

## MCP

AutoDoctor targets the current MCP Python SDK v2 transport. That SDK intentionally uses `httpx2`,
not `httpx`, for Streamable HTTP clients. AutoDoctor declares `httpx2` explicitly because
`mcp_backend.py` imports it directly.

Do not reuse an administrator's general-purpose token for future MCP work.
