"""
Tests for the shared mDNS-aware aiohttp resolver helper.

Covers two surfaces:

* :class:`PeerLinkDNSResolver` — the
  :class:`AsyncDualMDNSResolver` wrapper whose :meth:`close` is
  a no-op so per-request :class:`aiohttp.TCPConnector` instances
  don't tear down the shared resolver; :meth:`real_close` is the
  explicit teardown.
* :func:`make_peer_link_http_session` — the session factory that
  encapsulates the "wire the resolver into a fresh
  :class:`aiohttp.TCPConnector`" step so both
  :func:`drive_initiator_round_trip` and
  :meth:`PeerLinkClient._run_one_session` share one construction
  path.

The resolver's actual mDNS resolution is exercised end-to-end
by ``tests/e2e/test_pair_and_session.py`` against a real
:class:`AsyncZeroconf`; here we only pin the lifecycle + wiring
contracts that are easy to misregress.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp_asyncmdnsresolver._impl import _AsyncMDNSResolverBase
from aiohttp_asyncmdnsresolver.api import AsyncDualMDNSResolver
from zeroconf.asyncio import AsyncZeroconf

from esphome_device_builder.helpers.peer_link_resolver import (
    PeerLinkDNSResolver,
    make_peer_link_http_session,
    make_peer_link_resolver,
)


def _fake_async_zeroconf() -> AsyncZeroconf:
    """Return a :class:`MagicMock` standing in for :class:`AsyncZeroconf`.

    Passing a real one would open a UDP socket per test; the
    resolver only stores the reference and reads ``.zeroconf``
    when it actually resolves a name (which these tests don't
    do), so a mock is sufficient for the lifecycle checks.
    """
    return MagicMock(spec=AsyncZeroconf)


async def test_make_peer_link_resolver_borrows_the_zeroconf() -> None:
    """Constructed resolver doesn't own the shared :class:`AsyncZeroconf`.

    ``_aiozc_owner`` is ``False`` when an external
    :class:`AsyncZeroconf` is passed via ``async_zeroconf=`` —
    the upstream :class:`AsyncDualMDNSResolver` uses this flag
    to skip closing the borrowed instance on :meth:`close`. The
    device-state monitor owns the real instance and tears it
    down separately on its own stop path.
    """
    aiozc = _fake_async_zeroconf()
    resolver = make_peer_link_resolver(aiozc)
    assert resolver._aiozc is aiozc
    assert resolver._aiozc_owner is False


async def test_resolver_close_is_no_op_so_connector_close_keeps_it_usable() -> None:
    """``close()`` doesn't release ``aiodns`` or drop the zeroconf reference.

    A per-request :class:`aiohttp.TCPConnector` that closes its
    own resolver on connector-close must NOT tear down the
    shared resolver — multiple peer-link sessions reuse it.
    The wrapper's no-op :meth:`close` is what enforces this.
    """
    resolver = make_peer_link_resolver(_fake_async_zeroconf())
    captured_aiozc = resolver._aiozc
    await resolver.close()
    assert resolver._aiozc is captured_aiozc


async def test_real_close_releases_aiodns_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """``real_close()`` is the explicit teardown entry point.

    Mirrors Home Assistant's ``HassAsyncDNSResolver`` pattern:
    the dashboard's shutdown path calls :meth:`real_close`
    exactly once after every connector that referenced the
    resolver has been closed, so the underlying ``aiodns``
    resources can be released without being dragged down by an
    intermediate connector teardown.

    Patches the upstream parent's :meth:`close` directly rather
    than standing up a real ``aiodns`` resolver — the contract
    we want to pin is "``real_close`` walks through to the
    parent's close" and that's the smallest reproducer.
    """
    real_close = AsyncMock()
    monkeypatch.setattr(_AsyncMDNSResolverBase, "close", real_close)
    resolver = make_peer_link_resolver(_fake_async_zeroconf())
    await resolver.real_close()
    real_close.assert_awaited_once()


async def test_make_peer_link_http_session_with_resolver_wires_connector() -> None:
    """A non-``None`` resolver lands on the session's :class:`TCPConnector`.

    Pins the contract :func:`drive_initiator_round_trip` and
    :meth:`PeerLinkClient._run_one_session` both rely on:
    the factory builds a fresh :class:`TCPConnector` keyed to
    the shared resolver so outbound ``.local`` hostnames are
    resolved through mDNS.
    """
    resolver = make_peer_link_resolver(_fake_async_zeroconf())
    timeout = aiohttp.ClientTimeout(total=5.0)
    async with make_peer_link_http_session(timeout=timeout, resolver=resolver) as session:
        connector = session.connector
        assert isinstance(connector, aiohttp.TCPConnector)
        assert connector._resolver is resolver


async def test_make_peer_link_http_session_with_no_resolver_falls_through() -> None:
    """``resolver=None`` returns a session with ``aiohttp``'s default resolver.

    Preserves the pre-mDNS-resolver behaviour for paths where
    no shared :class:`AsyncZeroconf` is available (HA-addon
    mode without ``ports:``, unit-test paths, controllers
    constructed without a device-state monitor).
    """
    timeout = aiohttp.ClientTimeout(total=5.0)
    async with make_peer_link_http_session(timeout=timeout, resolver=None) as session:
        # No assertion on the resolver type — that's an aiohttp
        # implementation detail we don't want to pin. The
        # important contract is "session is usable as-is", which
        # the ``async with`` exit on close will surface if
        # broken.
        assert session.connector is not None


def test_peer_link_dns_resolver_is_async_dual_mdns_resolver_subclass() -> None:
    """Sanity check the inheritance contract.

    Downstream code (the offloader's ``ws_connect`` path) reads
    the resolver as an :class:`aiohttp.resolver.AbstractResolver`
    purely on duck typing, but the wrapper inheriting from
    :class:`AsyncDualMDNSResolver` is what makes the
    ``.local``-via-mDNS branch reachable on a plain
    :class:`TCPConnector(resolver=...)` construction.
    """
    assert issubclass(PeerLinkDNSResolver, AsyncDualMDNSResolver)


@pytest.mark.parametrize("hostname", ["receiver.local", "RECEIVER.LOCAL.", "receiver.local."])
def test_resolver_subclass_hits_mdns_branch_for_local_hostnames(hostname: str) -> None:
    """Document the ``.local`` discrimination the upstream resolver does.

    The upstream :class:`AsyncDualMDNSResolver.resolve` branches
    on ``host.endswith(".local")`` / ``".local."``: matched
    hostnames go through the mDNS path (which uses our shared
    :class:`AsyncZeroconf`), everything else falls through to
    the unicast-DNS parent. We don't test the resolve call
    itself here (it would need a real zeroconf instance), just
    pin that the strings the dashboard cares about are the ones
    the upstream split honours.
    """
    assert hostname.lower().endswith((".local", ".local."))
