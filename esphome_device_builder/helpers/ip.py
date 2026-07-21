"""Filters for unusable IP addresses from untrusted resolvers and payloads."""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import ip_address


def is_unspecified_address(value: str) -> bool:
    """Return True when *value* parses as an IP and is unspecified (``0.0.0.0`` / ``::``)."""
    try:
        return ip_address(value).is_unspecified
    except ValueError:
        return False


def is_usable_ip(value: str) -> bool:
    """Return True when *value* parses as an IP and is not unspecified."""
    try:
        return not ip_address(value).is_unspecified
    except ValueError:
        return False


def drop_unspecified_addresses(addresses: Iterable[str]) -> list[str]:
    """
    Drop unspecified entries from *addresses*.

    Entries that don't parse are kept — zeroconf hands out scoped
    IPv6 forms this filter must not eat.
    """
    return [address for address in addresses if not is_unspecified_address(address)]
