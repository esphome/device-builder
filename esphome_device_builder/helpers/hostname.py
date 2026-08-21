"""Hostname helpers shared between the DNS cache, ping sweep, and OTA cache args."""

from __future__ import annotations

import logging
from functools import lru_cache

from zeroconf import BadTypeInNameException, service_type_name

_LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def valid_mdns_service_name(name: str) -> bool:
    """
    Return True when *name* would construct a ``ServiceInfo`` without raising.

    The browser hands callbacks raw wire names; gate on this before
    building a ``ServiceInfo`` from one (#2620).
    """
    try:
        service_type_name(name, strict=False)
    except BadTypeInNameException as err:
        _LOGGER.debug("Ignoring invalid mDNS service name %r: %s", name, err)
        return False
    return True


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
