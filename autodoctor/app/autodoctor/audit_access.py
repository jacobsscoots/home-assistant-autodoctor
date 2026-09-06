from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import web

from .dashboard import ingress_remote_allowed

_HASSIO_IPV4_NETWORK = ipaddress.ip_network("172.30.32.0/23")
_HASSIO_IPV6_NETWORK = ipaddress.ip_network("fd0c:ac1e:2100::/48")
_SUPERVISOR_INFO_URL = "http://supervisor/supervisor/info"
_QUALIFICATION_PATH = "/api/qualification"


def internal_addon_remote_allowed(remote: str | None) -> bool:
    """Return True only for a source address on Supervisor's internal app bridge."""

    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    return address in _HASSIO_IPV4_NETWORK or address in _HASSIO_IPV6_NETWORK


def _bearer_token(request: web.Request) -> str:
    raw = str(request.headers.get("Authorization") or "").strip()
    scheme, separator, token = raw.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


async def supervisor_token_has_api_access(token: str) -> bool:
    """Validate a caller token against a protected, non-secret Supervisor endpoint.

    Home Assistant grants Core API and Supervisor API permissions separately. The audit
    caller only needs to prove that its SUPERVISOR_TOKEN is valid for Supervisor API
    access; requiring Core API access incorrectly rejects legitimate internal apps that
    can already read Supervisor state. The response body is discarded.

    The token is never logged, persisted, echoed, or returned. This check authorizes
    only the sanitized read-only qualification route below.
    """

    if not token:
        return False
    timeout = aiohttp.ClientTimeout(total=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _SUPERVISOR_INFO_URL,
                headers={"Authorization": f"Bearer {token}"},
                allow_redirects=False,
            ) as response:
                return response.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False


@web.middleware
async def ingress_or_authenticated_qualification(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Keep ingress-only UI while permitting authenticated app-to-app audit reads."""

    if ingress_remote_allowed(request.remote):
        return await handler(request)

    token = _bearer_token(request)
    if (
        request.method == "GET"
        and request.path == _QUALIFICATION_PATH
        and internal_addon_remote_allowed(request.remote)
        and token
        and await supervisor_token_has_api_access(token)
    ):
        return await handler(request)

    raise web.HTTPForbidden(
        text="AutoDoctor dashboard requires Home Assistant ingress; qualification audit access requires an authenticated internal app token."
    )
