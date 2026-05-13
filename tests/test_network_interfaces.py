"""Tests for :func:`helpers.network_interfaces.resolve_bind_host`.

Pins the contract: an IP literal or hostname is returned
unchanged; a known interface name expands to its IPv4 + IPv6
addresses (link-local IPv6 gets the kernel-reported zone suffix);
a known interface with no bindable address raises ``OSError``
rather than silently falling through to a bind on nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from esphome_device_builder.helpers.network_interfaces import resolve_bind_host


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
