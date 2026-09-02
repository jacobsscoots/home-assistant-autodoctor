# AutoDoctor v0.2.0 read-only MCP

AutoDoctor v0.2.0 can optionally enrich AI diagnoses from `ganhammar/hass-mcp-server`, but it does **not** expose MCP tools to the model and it does **not** add repair execution.

## Security model

The MCP client is fail-closed:

- `mcp_enabled` defaults to `false`.
- AutoDoctor has a compiled `READ_ONLY_TOOLS` allowlist.
- A tool name that is not on that allowlist is rejected locally before any MCP network request is made.
- Unknown future tools are denied by default.
- The AI provider never receives a generic tool-calling interface and cannot select MCP tools.
- Automatic v0.2.0 enrichment calls only `get_config`, `get_system_status`, and `list_integrations` every five minutes.
- MCP results are recursively bounded, entity IDs are replaced with `<ENTITY>`, known secrets/tokens are removed, and the normal AutoDoctor redactor is applied before data enters AI context.
- MCP call arguments and results are deliberately omitted from the audit log.
- MCP failures degrade to ordinary diagnosis without MCP context; they do not stop incident monitoring.

The upstream MCP server may expose write-capable tools. Their presence does not grant AutoDoctor permission to call them. AutoDoctor's application boundary denies tools such as service calls, event firing, automation/script/scene/helper CRUD, statistics mutation, config-file writes/backups/restores, dashboard writes, Home Assistant restart, and unknown tools.

## Allowlisted diagnostic reads

The v0.2.0 allowlist includes only read-oriented tools in these groups:

- entity/state discovery: `get_state`, `batch_get_state`, `list_entities`, `search_entities`, `get_device_details`;
- history/statistics reads: `list_calendar_events`, `get_history`, `get_logbook`, `get_statistics`, `list_statistic_ids`, `validate_statistics`;
- automation/scene/script reads: list/get config plus trace reads;
- helper reads: `list_helpers`, `get_helper_config`;
- system metadata: `get_config`, `get_system_status`, `get_domain_stats`, `get_error_log`, areas/devices/services/integrations/labels.

Being allowlisted does not mean a tool is automatically called. v0.2.0 automatic prompt enrichment intentionally uses only the three small system/integration reads listed above.

## Audit log

Every actual MCP tool attempt records a sanitized JSON line in:

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

Arguments, response payloads, tokens, and API keys are not stored in the audit log.

## Configuration

Keep MCP disabled until the Home Assistant MCP server is installed and its HTTP endpoint is known.

Typical configuration uses:

- `mcp_enabled: true`
- `mcp_url: http://<home-assistant-host>:8123/api/mcp_http` (or the endpoint actually exposed by the installed MCP integration)
- `mcp_token: <dedicated Home Assistant long-lived access token>`

The URL must be absolute HTTP(S) and may not contain embedded credentials, query parameters, or fragments.

Use a dedicated Home Assistant user/token with the least permissions compatible with the required MCP reads. Home Assistant/MCP credentials may still technically have permissions broader than AutoDoctor's application allowlist; that is a residual credential-level risk. v0.2.0 therefore relies on both least-privilege account setup where possible and AutoDoctor's deterministic local deny-by-default boundary.

## What remains forbidden

v0.2.0 cannot intentionally:

- call Home Assistant services through MCP;
- set/delete entity states;
- fire events;
- create/update/delete automations, scripts, scenes, helpers, calendars, dashboards, KNX entities, or config files;
- mutate or clear statistics;
- create/restore/delete MCP config backups;
- reload or restart Home Assistant;
- run any unknown/generic MCP escape tool;
- auto-apply a repair.

`auto_apply_low_risk` remains non-functional and the repair executor remains hard-disabled.

## Acceptance expectations

Before any future repair-capable phase, verify at minimum:

1. enabled MCP connects successfully;
2. only allowlisted reads appear as successful calls in `/data/mcp_audit.log`;
3. explicit attempts to call representative write tools are denied locally with no remote tool execution;
4. MCP result redaction does not expose tokens, raw IP addresses, email addresses, or unaliased Home Assistant entity IDs to AI context;
5. MCP outage/timeout/auth failures do not break monitoring or AI diagnosis without MCP;
6. Home Assistant writes, reloads, restarts, repair actions, and automation/script execution attributable to AutoDoctor remain zero.
