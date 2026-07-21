"""Filters for unusable IP addresses from untrusted resolvers and payloads."""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv4Address, IPv6Address, ip_address


def is_unspecified_address(value: str) -> bool:
    """Return True when *value* parses as an IP and is unspecified (``0.0.0.0`` / ``::``)."""
    parsed = _parse(value)
    return parsed is not None and parsed.is_unspecified


def is_usable_ip(value: str) -> bool:
    """Return True when *value* parses as an IP and is not unspecified."""
    parsed = _parse(value)
    return parsed is not None and not parsed.is_unspecified


def drop_unspecified_addresses(addresses: Iterable[str]) -> list[str]:
    """
    Drop unspecified entries from *addresses*.

    Entries that don't parse are kept — zeroconf hands out scoped
    IPv6 forms this filter must not eat.
    """
    return [address for address in addresses if not is_unspecified_address(address)]


def _parse(value: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(value)
    except ValueError:
        return None
