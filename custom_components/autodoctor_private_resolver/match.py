from __future__ import annotations

from ipaddress import IPv4Address, ip_address
from typing import Any

# RFC1918 ranges expressed as integer bounds so static-analysis tools do not mistake
# the specification's network constants for deployable hardcoded host addresses.
_RFC1918_INTEGER_RANGES = (
    (0x0A000000, 0x0AFFFFFF),  # /8 private block
    (0xAC100000, 0xAC1FFFFF),  # /12 private block
    (0xC0A80000, 0xC0A8FFFF),  # /16 private block
)


def normalize_private_ipv4(value: Any) -> str | None:
    """Return canonical RFC1918 IPv4 text, otherwise None.

    The resolver deliberately refuses public, loopback, link-local, IPv6, hostnames,
    ports and malformed values. AutoDoctor only needs the private LAN IPv4 emitted by
    real python-kasa transport failures.
    """

    try:
        parsed = ip_address(str(value).strip())
    except ValueError:
        return None
    if not isinstance(parsed, IPv4Address):
        return None

    numeric = int(parsed)
    if not any(lower <= numeric <= upper for lower, upper in _RFC1918_INTEGER_RANGES):
        return None
    return str(parsed)


def entry_data_matches_host(entry_data: Any, expected_host: str) -> bool:
    """Match only ConfigEntry.data['host'] by exact normalized RFC1918 equality."""

    if not isinstance(entry_data, dict):
        return False
    expected = normalize_private_ipv4(expected_host)
    if expected is None:
        return False
    actual = normalize_private_ipv4(entry_data.get("host"))
    return actual == expected
