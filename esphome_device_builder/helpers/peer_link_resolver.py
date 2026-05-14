"""
Shared aiohttp resolver + session factory for the outbound peer-link client.

The peer-link client (offloader-side) connects to receivers whose
hostnames are typically advertised over mDNS as ``*.local`` (the
LAN browse path) or persisted from an earlier discovery into
:attr:`~models.remote_build.StoredPairing.receiver_hostname`. The
host OS's ``getaddrinfo`` may or may not have mDNS wired in —
Linux without ``nss-mdns``, headless macOS without an Avahi
shim, and most container deployments resolve ``.local`` names
through unicast DNS only and fall through to ``NXDOMAIN``. That
silently breaks every outbound connect to an mDNS-only peer.

This module wires the existing :class:`AsyncEsphomeZeroconf`
instance the :class:`~controllers._device_state_monitor.DeviceStateMonitor`
already owns into an :class:`AsyncDualMDNSResolver` so the
outbound peer-link client's ``aiohttp.ClientSession`` resolves
``.local`` names through that shared Zeroconf rather than the OS
resolver. Non-``.local`` hostnames fall through to the regular
DNS path.

Sharing the resolver across all peer-link sessions matters for
two reasons:

* Only one mDNS responder can bind ``5353/udp`` per process —
  constructing a fresh :class:`AsyncZeroconf` per session would
  fight the device-state monitor's responder over the socket.
* The :class:`AsyncDualMDNSResolver`'s cache is per-instance; a
  shared instance amortises the lookup across the
  :func:`drive_initiator_round_trip` + :class:`PeerLinkClient`
  flows (preview, request_pair, pair_status long-poll, the
  long-lived peer-link session) which all hit the same handful
  of receiver hostnames.

The resolver's :meth:`close` is intentionally a no-op so the
:class:`aiohttp.TCPConnector` that owns it during a request can
be closed without tearing down the underlying
:class:`AsyncZeroconf` (which the device-state monitor still
needs). :meth:`real_close` performs the actual teardown of the
``aiodns`` side; the shared zeroconf is closed separately by
the monitor's stop path because the resolver was constructed
with ``async_zeroconf=`` (so
:attr:`AsyncDualMDNSResolver._aiozc_owner` is ``False``).

Mirrors Home Assistant core's ``HassAsyncDNSResolver`` +
``_async_create_clientsession`` pattern in
``homeassistant/helpers/aiohttp_client.py``: a single resolver
is wired once, and a session-factory helper builds
:class:`aiohttp.ClientSession` instances with the resolver
pre-attached so call sites don't repeat the
``TCPConnector(resolver=...)`` construction. HA gets to share
one :class:`aiohttp.ClientSession` across all requests because
its timeouts live on each ``session.get(...)`` call; our
per-call timeouts (10s short round-trip vs unbounded peer-link
session vs 1h pair_status long-poll) are session-level in
``aiohttp`` so we share the resolver but build a fresh session
per call. The factory keeps the resolver-wiring step from
duplicating across call sites.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp_asyncmdnsresolver.api import AsyncDualMDNSResolver

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncZeroconf


class PeerLinkDNSResolver(AsyncDualMDNSResolver):
    """
    Shared aiohttp resolver for outbound peer-link sessions.

    Wraps :class:`AsyncDualMDNSResolver` so per-request
    :class:`aiohttp.TCPConnector` instances can be opened and
    closed independently of the resolver's lifetime — the
    connector's close path delegates to :meth:`close`, which is
    a no-op here. The dashboard's shutdown path calls
    :meth:`real_close` exactly once to release the underlying
    ``aiodns`` resources.
    """

    async def real_close(self) -> None:
        """Release the underlying ``aiodns`` resources."""
        await super().close()

    async def close(self) -> None:
        """No-op so per-request connectors don't tear down the shared resolver."""


class _SkipHostsResolver(AbstractResolver):
    """Wraps an aiohttp resolver; drops results whose host is in *skip_hosts*."""

    def __init__(self, inner: AbstractResolver, skip_hosts: set[str]) -> None:
        self._inner = inner
        self._skip_hosts = skip_hosts

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        return [r for r in results if r.get("host") not in self._skip_hosts]

    async def close(self) -> None:
        """No-op so per-request connector close doesn't kill the shared inner resolver."""


def make_peer_link_resolver(async_zeroconf: AsyncZeroconf) -> PeerLinkDNSResolver:
    """
    Build a :class:`PeerLinkDNSResolver` bound to *async_zeroconf*.

    The resolver does **not** take ownership of the
    :class:`AsyncZeroconf` instance — the caller (the device-
    state monitor) keeps it for the LAN browser path; the
    resolver borrows it for ``.local`` lookups only.
    :attr:`AsyncDualMDNSResolver._aiozc_owner` resolves to
    ``False`` because we pass the instance via the
    ``async_zeroconf=`` keyword.
    """
    return PeerLinkDNSResolver(async_zeroconf=async_zeroconf)


def make_peer_link_http_session(
    *,
    timeout: aiohttp.ClientTimeout,
    resolver: AbstractResolver | None,
) -> aiohttp.ClientSession:
    """
    Build an :class:`aiohttp.ClientSession` for one peer-link call.

    Encapsulates the "wire the shared resolver into a fresh
    :class:`~aiohttp.TCPConnector` if one is available" step so
    every outbound peer-link call site
    (:func:`drive_initiator_round_trip`,
    :meth:`PeerLinkClient._run_one_session`) shares a single
    construction path. When *resolver* is ``None`` (no shared
    zeroconf, tests, fallback paths) the session falls through
    to ``aiohttp``'s default OS resolver so the legacy plumbing
    behaviour is preserved.

    The caller owns the returned session — use it as an
    ``async with`` context manager and let it close the
    connector on exit. The resolver itself is shared and
    survives the connector's close (its :meth:`close` is a
    no-op; see :class:`PeerLinkDNSResolver`).
    """
    connector = aiohttp.TCPConnector(resolver=resolver) if resolver is not None else None
    return aiohttp.ClientSession(timeout=timeout, connector=connector)
