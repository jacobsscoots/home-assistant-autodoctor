# AutoDoctor Private Resolver

This Home Assistant custom integration is the deployable companion for AutoDoctor v0.4.9's private TP-Link target refinement.

It registers one admin-only, read-only WebSocket command:

`autodoctor_private_resolver/match_tplink_host`

The command accepts a literal RFC1918 IPv4 address, checks only Home Assistant `tplink` config entries, and compares only `ConfigEntry.data["host"]` using exact normalized equality.

The response is deliberately limited to the TP-Link domain, match count, matching config-entry IDs, and config-entry states. It does not return the queried host, arbitrary `ConfigEntry.data`, titles, credentials, MAC addresses, entity IDs, or device IDs.

There is no write operation, generic config-entry inspection API, LAN scan, ARP lookup, fuzzy match, or automatic repair capability in this component.

## Deployment

Copy this directory unchanged to:

`/config/custom_components/autodoctor_private_resolver/`

Add one top-level YAML entry if it is not already present:

```yaml
autodoctor_private_resolver:
```

Run Home Assistant's supported configuration check and restart Core so the WebSocket command is registered.
