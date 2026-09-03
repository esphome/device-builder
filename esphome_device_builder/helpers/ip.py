"""Filters for unusable IP addresses and ports from untrusted resolvers and payloads."""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv4Address, IPv6Address, ip_address


def is_ip_address(value: str) -> bool:
    """Return True when *value* parses as an IPv4 / IPv6 address."""
    return _parse(value) is not None


def port_or_zero(value: object) -> int:
    """Return *value* as a TCP port (1-65535) or 0 when absent / malformed."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 < value < 65536 else 0


def is_unusable_address(value: str) -> bool:
    """Return True when *value* parses as an IP and is unspecified or loopback."""
    parsed = _parse(value)
    return parsed is not None and _is_unusable(parsed)


def is_usable_ip(value: str) -> bool:
    """Return True when *value* parses as an IP and is neither unspecified nor loopback."""
    parsed = _parse(value)
    return parsed is not None and not _is_unusable(parsed)


def drop_unusable_addresses(addresses: Iterable[str]) -> list[str]:
    """
    Drop unspecified and loopback entries from *addresses*.

    Entries that don't parse as an IP are kept unchanged — the
    filter only removes recognizable unusable addresses.
    """
    return [address for address in addresses if not is_unusable_address(address)]


def _is_unusable(parsed: IPv4Address | IPv6Address) -> bool:
    return parsed.is_unspecified or parsed.is_loopback


def _parse(value: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(value)
    except ValueError:
        return None
