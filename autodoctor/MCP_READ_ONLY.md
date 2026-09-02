# AutoDoctor v0.2.1 read-only MCP

AutoDoctor v0.2.1 can optionally enrich AI diagnoses from a supported Home Assistant MCP server, but it does **not** expose MCP tools to the model and it does **not** add repair execution.

## Supported MCP profiles

v0.2.1 recognizes two explicit tool profiles after listing the live server catalog:

1. **homeassistant-ai/ha-mcp add-on** — `ha_*` tool names and the add-on's `/private_...` secret-path endpoint. A bearer token is not required for this profile because the unguessable path is the add-on credential.
2. **ganhammar/hass-mcp-server style** — the earlier unprefixed tool names and bearer-token authentication retained for compatibility.

Any other/unknown tool profile fails closed and contributes no MCP context.

## Security model

The MCP client is fail-closed:

- `mcp_enabled` defaults to `false`.
- AutoDoctor has a compiled `READ_ONLY_TOOLS` allowlist for each supported profile.
- A tool name that is not on that allowlist is rejected locally before any MCP network tool call is made.
- Unknown future tools are denied by default.
- For the `ha-mcp` profile, the live catalog must additionally declare `readOnlyHint=True` for an allowlisted tool before AutoDoctor may call it.
- The AI provider never receives a generic tool-calling interface and cannot select MCP tools or supply MCP arguments.
- Automatic `ha-mcp` enrichment calls only `ha_get_overview` with `detail_level="minimal"` every five minutes.
- Automatic legacy-profile enrichment calls only `get_system_status` and `list_integrations`.
- MCP results are recursively bounded; entity IDs, IP/email data, known secrets/tokens, coordinates, location/area/floor/zone names and friendly names are redacted before data enters AI context.
- MCP call arguments and results are deliberately omitted from the audit log.
- MCP failures clear cached MCP context and degrade to ordinary diagnosis; they do not stop incident monitoring.

The upstream MCP server may expose write-capable tools. Their presence does not grant AutoDoctor permission to call them.

### Defence in depth with homeassistant-ai/ha-mcp

The `homeassistant-ai/ha-mcp` project also provides its own **Read Only Mode**. When enabled, its server filters write-capable tools from the catalog and blocks writes again at call time. AutoDoctor does not rely on that external mode for its own safety boundary, but enabling it is recommended when the MCP add-on is dedicated to AutoDoctor because it provides an independent second enforcement layer.

Do not enable the upstream server's global Read Only Mode blindly if other MCP clients intentionally use the same server to control Home Assistant; it applies to those clients too.

## Allowlisted diagnostic reads

The `ha-mcp` profile intentionally starts with a small verified set:

- `ha_get_overview`
- `ha_get_state`
- `ha_search`
- `ha_get_system_health`
- `ha_get_integration`
- `ha_get_history`
- `ha_get_logs`
- `ha_get_automation_traces`
- `ha_get_device`
- `ha_get_entity`
- `ha_list_services`

Every one still requires `readOnlyHint=True` from the live `ha-mcp` catalog before use.

The legacy profile retains the v0.2.0 read-oriented allowlist for entity/state, history/statistics, automation/script/scene reads, helper reads and system metadata.

Being allowlisted does not mean a tool is automatically called.

## Authentication

### homeassistant-ai/ha-mcp add-on

The add-on publishes a URL such as:

`http://<home-assistant-host>:9583/private_<random-secret>`

The `/private_...` path is the credential. Configure the complete URL as `mcp_url` and leave `mcp_token` empty.

AutoDoctor accepts tokenless MCP only when the first URL path segment begins with `/private_` and is non-empty. Arbitrary unauthenticated HTTP MCP URLs are rejected.

The full URL and secret path are scrubbed from AutoDoctor errors and audit records. `mcp_url` is also defined as a password-style add-on option so the Home Assistant options UI masks it.

### Bearer-token MCP servers

For compatible bearer-token servers, configure an absolute HTTP(S) `mcp_url` and set `mcp_token` to the dedicated token.

URLs with embedded username/password credentials, query parameters or fragments are rejected.

## Audit log

Every AutoDoctor MCP tool attempt records a sanitized JSON line in:

`/data/mcp_audit.log`

The file rotates at 1 MB and keeps three backups. Each record contains:

- timestamp;
- random correlation ID;
- tool name;
- allow/deny decision;
- diagnostic purpose;
- duration;
- success/failure;
- sanitized error summary.

Arguments, response payloads, bearer tokens, secret-path credentials and API keys are not stored in the audit log. Locally rejected attempts are auditable without opening an MCP network session.

## What remains forbidden

v0.2.1 cannot intentionally:

- call Home Assistant services through MCP;
- set/delete entity states;
- fire events;
- create/update/delete automations, scripts, scenes, helpers, integrations or config files;
- mutate dashboards/statistics;
- reload or restart Home Assistant;
- run an unknown/generic MCP escape tool;
- auto-apply a repair.

`auto_apply_low_risk` remains non-functional and the repair executor remains hard-disabled.

## Acceptance expectations

Before any future repair-capable phase, verify at minimum:

1. the live MCP server is positively identified as a supported profile;
2. `ha-mcp` secret-path authentication connects without a bearer token when that profile is used;
3. `ha-mcp` allowlisted reads are also marked `readOnlyHint=True` by the live catalog;
4. automatic `ha-mcp` successful calls are limited to `ha_get_overview(detail_level="minimal")`;
5. explicit attempts to call representative write or unknown tools are denied locally with no remote tool execution;
6. audit records contain no arguments, response payloads or credentials;
7. MCP result redaction does not expose tokens, secret URLs, raw IP/email data, entity IDs, location coordinates, areas/floors/zones or friendly names to AI context;
8. MCP outage/timeout/auth/profile failures clear cached MCP context and do not break monitoring;
9. Home Assistant writes, reloads, restarts, repair actions and automation/script execution attributable to AutoDoctor remain zero.
