from __future__ import annotations

import logging

# HTTPX2 logs complete request URLs at INFO. For ha-mcp secret-path authentication,
# the request path is itself a credential, so third-party transport logs must never
# reach container stdout. AutoDoctor already emits its own sanitized MCP errors/audit.
_SENSITIVE_HTTP_LOGGER_PREFIXES = ("httpx2", "httpcore2")
_SILENT_LEVEL = logging.CRITICAL + 1


def suppress_sensitive_http_transport_logs() -> None:
    """Disable HTTPX2/httpcore2 records that may contain credential-bearing URLs."""
    for prefix in _SENSITIVE_HTTP_LOGGER_PREFIXES:
        logging.getLogger(prefix).setLevel(_SILENT_LEVEL)

    # Also harden any transport child loggers already created before startup.
    for name in tuple(logging.root.manager.loggerDict):
        if any(name.startswith(f"{prefix}.") for prefix in _SENSITIVE_HTTP_LOGGER_PREFIXES):
            logging.getLogger(name).setLevel(_SILENT_LEVEL)
