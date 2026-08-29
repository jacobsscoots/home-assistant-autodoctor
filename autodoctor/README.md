# AutoDoctor

AutoDoctor is an AI-assisted reliability app for Home Assistant. It watches the native
`system_log_event` stream, fingerprints recurring errors, stores incident history, gathers
bounded live context, and asks an AI for a conservative diagnosis and repair proposal.

## Source of truth

AutoDoctor must treat the **live Home Assistant instance** as its source of truth.
Repository copies of `automations.yaml`, `scripts.yaml`, `configuration.yaml`, helpers, or entity
inventories may be stale and must not be used to infer current entity IDs, automation IDs,
script keys, services, or repair targets.

v0.1 therefore reads live events and live entity states only. It does not mount `/config` and
does not read the repository's Home Assistant YAML files at runtime.

Future repair versions must resolve targets from live Home Assistant/MCP data immediately before
a change and must refuse to repair when the live target cannot be resolved unambiguously.

## v0.1 safety boundary

**v0.1 cannot modify Home Assistant.** This is deliberate. The `auto_apply_low_risk` option
is visible so the policy can be exercised, but the executor contains a hard deny until the
MCP backup/validation/rollback flow is tested against the target Home Assistant instance.

This first release proves the always-on loop safely:

`system_log_event -> dedupe -> context -> AI -> incident record -> dashboard`

Optional MCP support uses `ganhammar/hass-mcp-server` for connectivity and capability
discovery only in v0.1. The next phase will add explicit automation/script identifier resolution,
then use MCP backup, validation and restore tools rather than unrestricted filesystem access.

## Feedback-loop protection

AutoDoctor deliberately ignores log events whose logger name or message contains `autodoctor`.
This prevents AutoDoctor's own warnings from becoming new AutoDoctor incidents.

When performing a synthetic smoke test, do **not** put the word `autodoctor` in the test logger
or message. A neutral logger/message such as `ha_pipeline_smoke_test` and
`Synthetic monitor-only pipeline verification event` will exercise the pipeline correctly.

## Why it does not require Watchman or Spook

Both can be useful evidence sources, but AutoDoctor uses Home Assistant's native system log
stream as its primary detector. Third-party diagnostics can be added later without becoming
a single point of failure.

## AI providers

- `none`: monitor/deduplicate only, no external AI calls.
- `openai`: direct OpenAI API. `ai_model` must be set explicitly.
- `anthropic`: direct Anthropic API. `ai_model` must be set explicitly.

AutoDoctor intentionally has no baked-in AI model name so model/version changes cannot silently
change behaviour. API usage is billed separately from ChatGPT/Claude consumer subscriptions.

## MCP

For Claude/manual repair access and future safe execution, install
`ganhammar/hass-mcp-server`, enable native Home Assistant authentication, create a dedicated
Long-Lived Access Token, then configure its `/api/mcp` URL and token in AutoDoctor.

AutoDoctor targets the current MCP Python SDK v2 transport. That SDK intentionally uses
`httpx2`, not `httpx`, for Streamable HTTP clients. AutoDoctor declares `httpx2` explicitly
because `mcp_backend.py` imports it directly.

Do not reuse an administrator's general-purpose token.
