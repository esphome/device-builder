"""Tests for :mod:`helpers.network_interfaces`.

Pins the ``resolve_bind_host`` contract: an IP literal or hostname
is returned unchanged; a known interface name expands to its
IPv4 + IPv6 addresses (link-local IPv6 gets the kernel-reported
zone suffix); a known interface with no bindable address raises
``OSError`` rather than silently falling through to a bind on
nothing. Plus the ``bind_available_port`` scan contract: first
candidate free on every host wins and its sockets stay bound
(race-free reservation), abandoned candidates close theirs,
exhaustion and unexpected errnos raise.
"""

from __future__ import annotations

import errno
import socket
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import get_unused_port_socket

from esphome_device_builder.helpers import network_interfaces
from esphome_device_builder.helpers.network_interfaces import (
    bind_available_port,
    resolve_bind_host,
)


def _ipv4(address: str) -> SimpleNamespace:
    """Ifaddr ``IP``-shaped stub for an IPv4 address."""
    return SimpleNamespace(ip=address, is_IPv4=True, is_IPv6=False, network_prefix=24)


def _ipv6(address: str, scope_id: int = 0) -> SimpleNamespace:
    """Ifaddr ``IP``-shaped stub for an IPv6 address (tuple form: addr, flowinfo, scope_id)."""
    return SimpleNamespace(
        ip=(address, 0, scope_id),
        is_IPv4=False,
        is_IPv6=True,
        network_prefix=64,
    )


def _adapter(name: str, *, index: int = 1, ips: list[SimpleNamespace]) -> SimpleNamespace:
    """Ifaddr ``Adapter``-shaped stub (only the fields we read)."""
    return SimpleNamespace(name=name, nice_name=name, index=index, ips=ips)


def _patch_adapters(*adapters: SimpleNamespace) -> object:
    return patch(
        "esphome_device_builder.helpers.network_interfaces.ifaddr.get_adapters",
        return_value=list(adapters),
    )


def test_unknown_interface_returns_literal_as_singleton() -> None:
    """A literal IP / hostname is wrapped in a single-element list."""
    with _patch_adapters(_adapter("eth0", ips=[_ipv4("192.168.1.10")])):
        assert resolve_bind_host("127.0.0.1") == ["127.0.0.1"]
        assert resolve_bind_host("0.0.0.0") == ["0.0.0.0"]
        assert resolve_bind_host("dashboard.lan") == ["dashboard.lan"]


def test_ipv4_interface_expands() -> None:
    """Interface name → its IPv4 address list."""
    with _patch_adapters(
        _adapter("eth0", ips=[_ipv4("192.168.1.10"), _ipv4("10.0.0.5")]),
    ):
        assert resolve_bind_host("eth0") == ["192.168.1.10", "10.0.0.5"]


def test_ipv6_addresses_carry_their_scope_id() -> None:
    """A non-zero scope_id from the kernel tuple is suffixed; zero is left bare."""
    with _patch_adapters(
        _adapter(
            "eth0",
            ips=[
                _ipv6("fe80::1", scope_id=2),
                _ipv6("2001:db8::1", scope_id=0),
            ],
        ),
    ):
        assert resolve_bind_host("eth0") == ["fe80::1%2", "2001:db8::1"]


def test_interface_without_ip_raises() -> None:
    """A known interface that carries no IPv4/IPv6 refuses to start."""
    with (
        _patch_adapters(_adapter("eth0", ips=[])),
        pytest.raises(OSError, match="no bindable IPv4/IPv6"),
    ):
        resolve_bind_host("eth0")


def _close_all(sockets: list[socket.socket]) -> None:
    for sock in sockets:
        sock.close()


def test_bind_available_port_returns_start_when_free() -> None:
    """A free start port is bound and returned with its socket held."""
    with get_unused_port_socket("127.0.0.1") as probe:
        port = probe.getsockname()[1]
    found, sockets = bind_available_port(["127.0.0.1"], port, 10)
    try:
        assert found == port
        assert [sock.getsockname()[1] for sock in sockets] == [port]
    finally:
        _close_all(sockets)


def test_bind_available_port_skips_occupied_port() -> None:
    """An actively-listened start port falls forward within the scan range."""
    with get_unused_port_socket("127.0.0.1") as blocker:
        blocker.listen(1)
        port = blocker.getsockname()[1]
        found, sockets = bind_available_port(["127.0.0.1"], port, 10)
        try:
            assert found in range(port + 1, port + 10)
            assert [sock.getsockname()[1] for sock in sockets] == [found]
        finally:
            _close_all(sockets)


def test_bind_available_port_binds_ipv6_host() -> None:
    """An IPv6 host gets a v6-only socket and the OS-assigned port is reported."""
    if not socket.has_ipv6:
        pytest.skip("IPv6 unavailable")
    try:
        found, sockets = bind_available_port(["::1"], 0, 1)
    except OSError as err:
        pytest.skip(f"IPv6 loopback unavailable: {err}")
    try:
        assert found == sockets[0].getsockname()[1] != 0
        assert sockets[0].family == socket.AF_INET6
        assert sockets[0].getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
    finally:
        _close_all(sockets)


@pytest.mark.parametrize(
    ("start_port", "attempts", "match"),
    [
        pytest.param(6055, 0, "attempts must be >= 1", id="zero_attempts"),
        pytest.param(6055, -3, "attempts must be >= 1", id="negative_attempts"),
        pytest.param(-1, 10, "start_port must be 0-65535", id="negative_port"),
        pytest.param(99999, 10, "start_port must be 0-65535", id="port_too_high"),
    ],
)
def test_bind_available_port_rejects_invalid_arguments(
    start_port: int, attempts: int, match: str
) -> None:
    """An empty or out-of-range scan raises ValueError instead of a nonsense range."""
    with pytest.raises(ValueError, match=match):
        bind_available_port(["127.0.0.1"], start_port, attempts)


def test_bind_available_port_raises_when_exhausted() -> None:
    """A scan with no free candidate raises EADDRINUSE naming the range."""
    with get_unused_port_socket("127.0.0.1") as blocker:
        blocker.listen(1)
        port = blocker.getsockname()[1]
        with pytest.raises(OSError, match=f"No free port in {port}-{port}") as excinfo:
            bind_available_port(["127.0.0.1"], port, 1)
        assert excinfo.value.errno == errno.EADDRINUSE


def test_bind_available_port_abandons_candidate_when_any_host_fails() -> None:
    """A candidate taken on any host is abandoned everywhere and its sockets closed."""
    calls: list[tuple[str, int]] = []
    handed_out: dict[tuple[str, int], MagicMock] = {}

    def _fake_bind(host: str, port: int) -> list[MagicMock]:
        calls.append((host, port))
        if (host, port) == ("h2", 6055):
            raise OSError(errno.EADDRINUSE, "taken")
        sock = MagicMock()
        sock.getsockname.return_value = (host, port)
        handed_out[(host, port)] = sock
        return [sock]

    with patch.object(network_interfaces, "_bind_sockets", _fake_bind):
        found, sockets = bind_available_port(["h1", "h2"], 6055, 10)
    assert found == 6056
    assert calls == [("h1", 6055), ("h2", 6055), ("h1", 6056), ("h2", 6056)]
    # The abandoned candidate's socket was closed; the winners weren't.
    handed_out[("h1", 6055)].close.assert_called_once()
    assert sockets == [handed_out[("h1", 6056)], handed_out[("h2", 6056)]]
    for sock in sockets:
        sock.close.assert_not_called()


@pytest.mark.parametrize(
    ("err_errno", "message"),
    [
        pytest.param(errno.EADDRNOTAVAIL, "bad host", id="bad_host"),
        pytest.param(
            errno.EACCES,
            "denied",
            id="privileged_port",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="Windows EACCES means an excluded port range"
            ),
        ),
    ],
)
def test_bind_available_port_propagates_unexpected_oserror(err_errno: int, message: str) -> None:
    """A non-availability errno aborts the scan instead of falling forward."""
    calls: list[tuple[str, int]] = []

    def _fake_bind(host: str, port: int) -> list[MagicMock]:
        calls.append((host, port))
        raise OSError(err_errno, message)

    with (
        patch.object(network_interfaces, "_bind_sockets", _fake_bind),
        pytest.raises(OSError, match=message),
    ):
        bind_available_port(["203.0.113.1"], 6055, 10)
    assert calls == [("203.0.113.1", 6055)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows excluded-port-range semantics")
def test_bind_available_port_skips_access_denied_on_windows() -> None:
    """EACCES (an excluded port range) falls forward on Windows."""

    def _fake_bind(host: str, port: int) -> list[MagicMock]:
        if port == 6055:
            raise OSError(errno.EACCES, "excluded range")
        sock = MagicMock()
        sock.getsockname.return_value = (host, port)
        return [sock]

    with patch.object(network_interfaces, "_bind_sockets", _fake_bind):
        found, sockets = bind_available_port(["127.0.0.1"], 6055, 10)
    assert found == 6056
    assert len(sockets) == 1
