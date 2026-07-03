"""Hostname helpers shared between the DNS cache, ping sweep, and OTA cache args."""

from __future__ import annotations


def default_mdns_address(name: str) -> str:
    """Return the mDNS address ESPHome derives from a device *name* by default."""
    return f"{name}.local"


def normalize_hostname(hostname: str) -> str:
    """
    Lower-case *hostname* and strip the trailing FQDN dot.

    mDNS / DNS hostnames are case-insensitive and zeroconf often hands
    us names with a trailing ``.`` ; normalising once means cache keys
    and ``.local`` checks compare equal regardless of which form the
    caller passed in.
    """
    return hostname.rstrip(".").lower()


def is_local_hostname(hostname: str) -> bool:
    """Return True when *hostname* is an mDNS ``.local`` name (case/dot insensitive)."""
    return normalize_hostname(hostname).endswith(".local")
