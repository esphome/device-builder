"""Tests for the ping sweep's resolve-first mDNS step (issue #1993)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module
from esphome_device_builder.controllers._device_state_monitor import shared
from esphome_device_builder.models import DeviceState, ReachabilitySource

from .conftest import make_online_api_device, make_state_monitor_with_callbacks

_SERVICE_NAME = "kitchen._esphomelib._tcp.local."


def _stub_service_info(
    monkeypatch: pytest.MonkeyPatch, *, cached: bool, resolved: bool = False
) -> MagicMock:
    """Stub ``AsyncServiceInfo`` so the resolve hits the cache, the wire, or misses."""
    info = MagicMock()
    info.name = _SERVICE_NAME
    info.load_from_cache.return_value = cached
    info.async_request = AsyncMock(return_value=resolved)
    info.parsed_scoped_addresses.return_value = ["192.168.1.50"]
    info.decoded_properties = {"version": "2026.7.0", "config_hash": "abcd1234"}
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: info)
    return info


def _prime_sweep(monitor: Any, *, cache_trace: bool = True, live_ptr: bool = False) -> None:
    """Wire the fake zeroconf plus the two cache reads the sweep filter makes."""
    monitor._mdns._zeroconf = MagicMock()
    monitor.get_mdns_cache_info = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock() if cache_trace else None
    )
    monitor.has_live_mdns_ptr = MagicMock(return_value=live_ptr)  # type: ignore[method-assign]


async def test_sweep_claims_mdns_for_ping_owned_online_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #1993 repro: ping owns the ledger, the cache resolves → mdns reclaims, no wire."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_sweep(monitor)
    info = _stub_service_info(monkeypatch, cached=True)

    await shared.resolve_api_mdns_targets(monitor)

    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS
    assert ("on_source_change", "kitchen", ReachabilitySource.MDNS) in callbacks.calls
    assert device.runtime_state.deployed_version == "2026.7.0"
    assert device.runtime_state.deployed_config_hash == "abcd1234"
    assert device.runtime_state.state == DeviceState.ONLINE
    info.async_request.assert_not_called()


async def test_sweep_cache_miss_falls_back_to_wire_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_sweep(monitor)
    info = _stub_service_info(monkeypatch, cached=False, resolved=True)

    await shared.resolve_api_mdns_targets(monitor)

    info.async_request.assert_awaited_once()
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS


async def test_sweep_resolve_miss_claims_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A miss leaves the ledger and state alone — ICMP decides, never the resolve."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_sweep(monitor)
    _stub_service_info(monkeypatch, cached=False, resolved=False)

    await shared.resolve_api_mdns_targets(monitor)

    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING
    assert device.runtime_state.state == DeviceState.ONLINE
    assert callbacks.calls_for("on_state_change") == []
    assert shared.should_ping(monitor, device) is True


@pytest.mark.parametrize("state", [DeviceState.OFFLINE, DeviceState.UNKNOWN])
async def test_sweep_never_resolves_not_online_devices(state: DeviceState) -> None:
    """Ownership repair only — a not-online device must not come online off the cache (#1776)."""
    device = make_online_api_device(state=state)
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    _prime_sweep(monitor)
    monitor._mdns.resolve_and_claim = AsyncMock()  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)

    monitor._mdns.resolve_and_claim.assert_not_called()


@pytest.mark.parametrize(
    ("overrides", "source"),
    [
        pytest.param(
            {"api_enabled": False, "loaded_integrations": ["mqtt", "wifi"]},
            ReachabilitySource.PING,
            id="non_api",
        ),
        pytest.param({}, ReachabilitySource.MQTT, id="mqtt_owned"),
    ],
)
async def test_sweep_skips_ineligible_devices(
    overrides: dict[str, Any], source: ReachabilitySource
) -> None:
    device = make_online_api_device(**overrides)
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = source
    _prime_sweep(monitor)
    monitor._mdns.resolve_and_claim = AsyncMock()  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)

    monitor._mdns.resolve_and_claim.assert_not_called()


async def test_sweep_skips_devices_with_no_cache_trace() -> None:
    """mDNS-dark deployments (Docker bridge) must gain no multicast traffic."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_sweep(monitor, cache_trace=False)
    monitor._mdns.resolve_and_claim = AsyncMock()  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)

    monitor._mdns.resolve_and_claim.assert_not_called()


async def test_sweep_without_zeroconf_is_a_noop() -> None:
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    monitor._mdns._zeroconf = None
    monitor._mdns.resolve_and_claim = AsyncMock()  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)

    monitor._mdns.resolve_and_claim.assert_not_called()


async def test_sweep_rechecks_mdns_owned_device_without_live_ptr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-PTR mdns claim keeps sweep eligibility; a live PTR ends it (event-spam guard)."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_sweep(monitor, live_ptr=False)
    resolve = AsyncMock()
    monitor._mdns.resolve_and_claim = resolve  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)
    resolve.assert_awaited_once_with("kitchen")

    monitor.has_live_mdns_ptr = MagicMock(return_value=True)  # type: ignore[method-assign]
    await shared.resolve_api_mdns_targets(monitor)
    resolve.assert_awaited_once()


def test_should_ping_gates_mdns_ownership_on_live_ptr() -> None:
    """An mdns claim with no live PTR has no ``Removed`` counterpart — keep sweeping it."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS

    monitor.has_live_mdns_ptr = MagicMock(return_value=False)  # type: ignore[method-assign]
    assert shared.should_ping(monitor, device) is True

    monitor.has_live_mdns_ptr = MagicMock(return_value=True)  # type: ignore[method-assign]
    assert shared.should_ping(monitor, device) is False


def test_should_ping_non_api_mdns_ownership_unchanged() -> None:
    """Non-API devices never publish a PTR; their active-resolve claim keeps the lockout."""
    device = make_online_api_device(api_enabled=False, loaded_integrations=["mqtt", "wifi"])
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    monitor.has_live_mdns_ptr = MagicMock(return_value=False)  # type: ignore[method-assign]

    assert shared.should_ping(monitor, device) is False


def test_has_live_ptr_reads_the_browser_cache() -> None:
    monitor, _callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    fake_zeroconf = MagicMock()
    monitor._mdns._zeroconf = fake_zeroconf
    lookup = fake_zeroconf.zeroconf.cache.current_entry_with_name_and_alias

    ptr = MagicMock()
    ptr.is_expired.return_value = False
    lookup.return_value = ptr
    assert monitor.has_live_mdns_ptr("kitchen") is True
    lookup.assert_called_with("_esphomelib._tcp.local.", _SERVICE_NAME)

    ptr.is_expired.return_value = True
    assert monitor.has_live_mdns_ptr("kitchen") is False

    lookup.return_value = None
    assert monitor.has_live_mdns_ptr("kitchen") is False

    monitor._mdns._zeroconf = None
    assert monitor.has_live_mdns_ptr("kitchen") is False
