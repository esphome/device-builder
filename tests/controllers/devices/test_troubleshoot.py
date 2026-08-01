"""Coverage for the ``devices/troubleshoot`` on-demand probe."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    Device,
    DeviceRuntimeState,
    DeviceState,
    ErrorCode,
    ReachabilitySource,
)

from .conftest import MakeControllerFactory


def _seed_device(
    controller: Any,
    name: str = "kitchen",
    *,
    address: str = "kitchen.local",
    ip: str = "",
    ip_addresses: list[str] | None = None,
) -> Device:
    device = Device(
        name=name,
        friendly_name=name.title(),
        configuration=f"{name}.yaml",
        address=address,
        ip=ip,
        runtime_state=DeviceRuntimeState(ip_addresses=ip_addresses or []),
    )
    controller.get_by_configuration = lambda c: device if c == device.configuration else None
    return device


def _wire_monitor(
    controller: Any,
    *,
    icmp_available: bool | None = True,
    zeroconf_up: bool = True,
    dns_addresses: list[str] | None = None,
    dns_cached_failure: bool = False,
    mdns_cached: list[str] | None = None,
    has_trace: bool = False,
    live_ptr: bool = False,
    rtt: float | None = 5.0,
) -> MagicMock:
    monitor = MagicMock()
    monitor.ping.icmp_available = icmp_available
    monitor.ping.probe_target = AsyncMock(return_value=rtt)
    monitor.mdns.zeroconf = object() if zeroconf_up else None
    monitor.mdns.refresh_mdns = AsyncMock()
    monitor.mdns.get_cached_addresses = MagicMock(return_value=mdns_cached)
    monitor.mdns.has_cached_trace = MagicMock(return_value=has_trace)
    monitor.mdns.has_live_anchor_ptr = MagicMock(return_value=live_ptr)
    monitor.state.dns_cache.async_resolve = AsyncMock(return_value=dns_addresses)
    monitor.state.dns_cache.has_cached_failure = MagicMock(return_value=dns_cached_failure)
    controller._state_monitor = monitor
    return monitor


async def test_unknown_device_raises_not_found(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    controller.get_by_configuration = lambda _c: None
    _wire_monitor(controller)

    with pytest.raises(CommandError) as exc:
        await controller.troubleshoot_device(configuration="nope.yaml")
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_happy_path_wire_shape(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """DNS resolves, mDNS is live, ping answers; every field lands on the wire."""
    controller = make_controller(tmp_path)
    _seed_device(controller)
    monitor = _wire_monitor(
        controller,
        dns_addresses=["fe80::1", "10.0.0.42"],
        mdns_cached=["10.0.0.42"],
        has_trace=True,
        live_ptr=True,
        rtt=4.2,
    )

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    monitor.mdns.refresh_mdns.assert_awaited_once_with("kitchen")
    monitor.state.dns_cache.async_resolve.assert_awaited_once_with("kitchen.local")
    assert result.to_dict() == {
        "configuration": "kitchen.yaml",
        "address": "kitchen.local",
        "icmp_available": True,
        "zeroconf_running": True,
        "dns_resolved": True,
        "dns_addresses": ["fe80::1", "10.0.0.42"],
        "dns_had_cached_failure": False,
        "dns_inconclusive": False,
        "mdns_addresses": ["10.0.0.42"],
        "mdns_inconclusive": False,
        "mdns_has_cached_trace": True,
        "mdns_has_live_anchor_ptr": True,
        "ping_attempted": True,
        # IPv4 preferred over the scoped IPv6 the resolver ordered first.
        "ping_target": "10.0.0.42",
        "ping_target_source": "dns",
        "ping_rtt_ms": 4.2,
    }
    # The verdict routes through the shared sweep policy.
    monitor.ping.probe_target.assert_awaited_once()
    assert monitor.ping.probe_target.await_args.kwargs == {"apply": True}
    # Fresh resolve recorded like the sweep, and the cache dropped so
    # the resolve was live.
    monitor.apply_ip_addresses.assert_called_once_with("kitchen", ["fe80::1", "10.0.0.42"])
    monitor.state.dns_cache.invalidate.assert_called_once_with("kitchen.local")


async def test_zeroconf_down_degrades_mdns_fields(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    _seed_device(controller)
    monitor = _wire_monitor(
        controller, zeroconf_up=False, mdns_cached=None, dns_addresses=["10.0.0.42"]
    )
    monitor.mdns.has_cached_trace.return_value = False
    monitor.mdns.has_live_anchor_ptr.return_value = False

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.zeroconf_running is False
    assert result.mdns_addresses == []
    assert result.mdns_has_cached_trace is False


@pytest.mark.parametrize("icmp_available", [None, False], ids=["probing", "unavailable"])
async def test_icmp_unavailable_skips_ping(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    icmp_available: bool | None,
) -> None:
    controller = make_controller(tmp_path)
    _seed_device(controller)
    monitor = _wire_monitor(controller, icmp_available=icmp_available, dns_addresses=["10.0.0.42"])

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.icmp_available == icmp_available
    assert result.ping_attempted is False
    assert result.ping_target == ""
    monitor.ping.probe_target.assert_not_awaited()


async def test_ping_miss_applies_offline(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    _seed_device(controller)
    _wire_monitor(controller, dns_addresses=["10.0.0.42"], rtt=None)

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.ping_attempted is True
    assert result.ping_rtt_ms is None


async def test_dns_failure_falls_back_to_mdns_cache(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    _seed_device(controller)
    _wire_monitor(controller, dns_addresses=None, dns_cached_failure=True, mdns_cached=["10.0.0.7"])

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.dns_resolved is False
    assert result.dns_had_cached_failure is True
    assert result.ping_target == "10.0.0.7"
    assert result.ping_target_source == "mdns"


async def test_runtime_addresses_then_persisted_ip_fallbacks(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    device = _seed_device(controller, ip="10.0.0.9", ip_addresses=["10.0.0.8"])
    monitor = _wire_monitor(controller, dns_addresses=None, mdns_cached=None)

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")
    assert result.ping_target == "10.0.0.8"
    assert result.ping_target_source == "runtime"
    # RAM-learned addresses are sweep-grade evidence; the verdict applies.
    assert monitor.ping.probe_target.await_args.kwargs == {"apply": True}

    device.runtime_state.ip_addresses = []
    result = await controller.troubleshoot_device(configuration="kitchen.yaml")
    assert result.ping_target == "10.0.0.9"
    assert result.ping_target_source == "persisted"
    assert result.ping_rtt_ms is not None
    # A bare reply at the persisted IP is inadmissible (#1776): reported,
    # never applied.
    assert monitor.ping.probe_target.await_args.kwargs == {"apply": False}


async def test_no_target_skips_ping(tmp_path: Path, make_controller: MakeControllerFactory) -> None:
    controller = make_controller(tmp_path)
    _seed_device(controller)
    monitor = _wire_monitor(controller, dns_addresses=None, mdns_cached=None)

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.ping_attempted is False
    monitor.ping.probe_target.assert_not_awaited()


async def test_empty_address_skips_dns(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    _seed_device(controller, address="")
    monitor = _wire_monitor(controller, dns_cached_failure=True)

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.dns_resolved is False
    assert result.dns_had_cached_failure is False
    monitor.state.dns_cache.async_resolve.assert_not_awaited()
    monitor.state.dns_cache.has_cached_failure.assert_not_called()


async def test_resolver_exception_degrades(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A non-timeout resolver failure degrades instead of failing the command."""
    controller = make_controller(tmp_path)
    _seed_device(controller)
    monitor = _wire_monitor(controller, mdns_cached=["10.0.0.7"])
    monitor.state.dns_cache.async_resolve = AsyncMock(side_effect=OSError("resolver down"))

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.dns_resolved is False
    assert result.dns_inconclusive is True
    assert result.ping_target == "10.0.0.7"


async def test_mdns_refresh_exception_degrades(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    _seed_device(controller)
    monitor = _wire_monitor(controller, dns_addresses=["10.0.0.42"], rtt=4.2)
    monitor.mdns.refresh_mdns = AsyncMock(side_effect=RuntimeError("apply blew up"))

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.dns_resolved is True
    assert result.mdns_inconclusive is True
    assert result.ping_rtt_ms == 4.2


async def test_cancellation_unwinds(tmp_path: Path, make_controller: MakeControllerFactory) -> None:
    """A cancelled leg propagates instead of degrading into a verdict."""
    controller = make_controller(tmp_path)
    _seed_device(controller)
    monitor = _wire_monitor(controller)
    monitor.mdns.refresh_mdns = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await controller.troubleshoot_device(configuration="kitchen.yaml")


async def test_owned_online_device_keeps_its_addresses(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The sweep's ownership gate also guards the probe's address write."""
    controller = make_controller(tmp_path)
    device = _seed_device(controller)
    device.runtime_state.state = DeviceState.ONLINE
    monitor = _wire_monitor(controller, dns_addresses=["10.0.0.66"])
    monitor.state.state_source = {"kitchen": ReachabilitySource.MDNS}

    result = await controller.troubleshoot_device(configuration="kitchen.yaml")

    assert result.dns_resolved is True
    monitor.apply_ip_addresses.assert_not_called()
