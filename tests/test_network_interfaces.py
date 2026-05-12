"""Tests for :func:`helpers.network_interfaces.resolve_bind_host`.

Pins the contract: an IP literal or hostname is returned
unchanged; a known interface name expands to its IPv4 + IPv6
addresses (link-local IPv6 gets a zone-index suffix); a known
interface with no bindable address raises ``OSError`` rather
than silently falling through to a bind on nothing.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from esphome_device_builder.helpers.network_interfaces import resolve_bind_host


def _addr(family: int, address: str) -> SimpleNamespace:
    """Psutil ``snicaddr``-shaped stub (only the fields we read)."""
    return SimpleNamespace(family=family, address=address)


def test_unknown_interface_returns_literal_as_singleton() -> None:
    """A literal IP / hostname is wrapped in a single-element list."""
    with patch(
        "esphome_device_builder.helpers.network_interfaces.psutil.net_if_addrs",
        return_value={"eth0": [_addr(socket.AF_INET, "192.168.1.10")]},
    ):
        assert resolve_bind_host("127.0.0.1") == ["127.0.0.1"]
        assert resolve_bind_host("0.0.0.0") == ["0.0.0.0"]
        assert resolve_bind_host("dashboard.lan") == ["dashboard.lan"]


def test_ipv4_interface_expands() -> None:
    """Interface name → its IPv4 address list."""
    with patch(
        "esphome_device_builder.helpers.network_interfaces.psutil.net_if_addrs",
        return_value={
            "eth0": [
                _addr(socket.AF_INET, "192.168.1.10"),
                _addr(socket.AF_INET, "10.0.0.5"),
            ],
        },
    ):
        assert resolve_bind_host("eth0") == ["192.168.1.10", "10.0.0.5"]


def test_ipv6_link_local_gets_zone_suffix() -> None:
    """``fe80::/10`` addresses get ``%<ifindex>`` so the bind picks the scope."""
    with (
        patch(
            "esphome_device_builder.helpers.network_interfaces.psutil.net_if_addrs",
            return_value={
                "eth0": [
                    _addr(socket.AF_INET, "192.168.1.10"),
                    _addr(socket.AF_INET6, "fe80::1"),
                    _addr(socket.AF_INET6, "2001:db8::1"),
                ],
            },
        ),
        patch(
            "esphome_device_builder.helpers.network_interfaces.socket.if_nametoindex",
            return_value=2,
        ) as if_nametoindex,
    ):
        resolved = resolve_bind_host("eth0")
    assert resolved == ["192.168.1.10", "fe80::1%2", "2001:db8::1"]
    if_nametoindex.assert_called_once_with("eth0")


def test_ipv6_link_local_with_existing_zone_is_passed_through() -> None:
    """A ``%`` already present means psutil included the zone — don't re-suffix."""
    with (
        patch(
            "esphome_device_builder.helpers.network_interfaces.psutil.net_if_addrs",
            return_value={"eth0": [_addr(socket.AF_INET6, "fe80::1%eth0")]},
        ),
        patch(
            "esphome_device_builder.helpers.network_interfaces.socket.if_nametoindex",
        ) as if_nametoindex,
    ):
        assert resolve_bind_host("eth0") == ["fe80::1%eth0"]
    if_nametoindex.assert_not_called()


def test_link_layer_addresses_are_skipped() -> None:
    """MAC / AF_PACKET entries are dropped — only IPv4/IPv6 can be bound."""
    af_packet = getattr(socket, "AF_PACKET", -1)
    with patch(
        "esphome_device_builder.helpers.network_interfaces.psutil.net_if_addrs",
        return_value={
            "eth0": [
                _addr(af_packet, "aa:bb:cc:dd:ee:ff"),
                _addr(socket.AF_INET, "192.168.1.10"),
            ],
        },
    ):
        assert resolve_bind_host("eth0") == ["192.168.1.10"]


def test_interface_without_ip_raises() -> None:
    """A known interface that carries no IPv4/IPv6 refuses to start."""
    af_packet = getattr(socket, "AF_PACKET", -1)
    with (
        patch(
            "esphome_device_builder.helpers.network_interfaces.psutil.net_if_addrs",
            return_value={"eth0": [_addr(af_packet, "aa:bb:cc:dd:ee:ff")]},
        ),
        pytest.raises(OSError, match="no bindable IPv4/IPv6"),
    ):
        resolve_bind_host("eth0")
