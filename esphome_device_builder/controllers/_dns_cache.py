"""
TTL'd async A/AAAA resolver.

Caches DNS lookups so repeated pings and OTA operations against the
same hostname don't hammer the system resolver. Successful and failed
resolutions are both cached for the configured TTL — caching failures
keeps a transient outage from triggering a thundering herd of retries
across every ping cycle. Failed entries are hidden from
:meth:`get_cached_addresses` so callers fall through to their own
fallbacks.

This is intentionally separate from the zeroconf-backed mDNS cache
exposed by :class:`DeviceStateMonitor`: that one is event-driven and
mDNS-only, this one is a pull-based DNS resolver useful for non-mDNS
hostnames.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from ipaddress import ip_address

try:
    from icmplib import NameLookupError, async_resolve
except ImportError:  # pragma: no cover — icmplib is optional
    NameLookupError = Exception  # type: ignore[assignment, misc]
    async_resolve = None  # type: ignore[assignment]

from ..helpers.hostname import normalize_hostname

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 120
_RESOLVE_TIMEOUT_SECONDS = 3.0
# icmplib raises NameLookupError on resolution failure; UnicodeError
# fires for malformed hostnames; TimeoutError covers the asyncio
# timeout we wrap the call in.
_RESOLVE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    NameLookupError,
    UnicodeError,
)


class DNSCache:
    """
    TTL'd async A/AAAA resolver.

    Use :meth:`async_resolve` to look up a hostname, caching the result
    for *ttl* seconds. :meth:`get_cached_addresses` returns the cached
    IPs without triggering resolution — useful when building OTA cache
    args from data we already have on hand.

    Literal IPv4/IPv6 addresses short-circuit the cache entirely.
    """

    def __init__(self, ttl: int = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl
        # hostname → (expires_at_monotonic, addresses-or-None-on-failure)
        self._cache: dict[str, tuple[float, list[str] | None]] = {}

    def get_cached_addresses(self, hostname: str) -> list[str] | None:
        """
        Return cached IPs for *hostname* without triggering a lookup.

        ``None`` when the cache misses, the entry has expired, or the
        last resolution failed.
        """
        normalized = self._normalize(hostname)
        with suppress(ValueError):
            return [str(ip_address(normalized))]

        entry = self._cache.get(normalized)
        if entry is None:
            return None
        expires_at, addresses = entry
        if expires_at <= time.monotonic() or not addresses:
            return None
        return list(addresses)

    async def async_resolve(self, hostname: str) -> list[str] | None:
        """
        Resolve *hostname* to a list of IPs, caching the result.

        Returns ``None`` when resolution fails (the failure is also
        cached so retries don't hammer the resolver during an outage).
        Literal IPs are returned immediately without a lookup. When
        ``.local`` resolution fails, the bare hostname is tried as a
        fallback in case the network has unicast DNS for it.
        """
        normalized = self._normalize(hostname)
        with suppress(ValueError):
            return [str(ip_address(normalized))]

        if async_resolve is None:
            return None

        now = time.monotonic()
        entry = self._cache.get(normalized)
        if entry is not None and entry[0] > now:
            return list(entry[1]) if entry[1] else None

        addresses = await self._resolve(normalized)
        self._cache[normalized] = (now + self._ttl, addresses)
        return list(addresses) if addresses else None

    _normalize = staticmethod(normalize_hostname)

    async def _resolve(self, hostname: str) -> list[str] | None:
        """Resolve *hostname* with a ``.local`` → bare-hostname fallback."""
        addresses = await self._try_resolve(hostname)
        if addresses is not None:
            return addresses
        # Some networks resolve the bare hostname via unicast DNS even
        # when ``.local`` mDNS resolution fails — fall back to that
        # rather than giving up immediately.
        if hostname.endswith(".local"):
            bare = hostname.removesuffix(".local")
            addresses = await self._try_resolve(bare)
            if addresses is not None:
                _LOGGER.debug("Resolved %s via bare-hostname fallback (%s)", hostname, bare)
        return addresses

    @staticmethod
    async def _try_resolve(hostname: str) -> list[str] | None:
        try:
            async with asyncio.timeout(_RESOLVE_TIMEOUT_SECONDS):
                return await async_resolve(hostname)
        except _RESOLVE_EXCEPTIONS:
            return None
