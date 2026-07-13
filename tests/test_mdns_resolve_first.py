"""Tests for the ping sweep's resolve-first mDNS step."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module
from esphome_device_builder.controllers._device_state_monitor import shared
from esphome_device_builder.models import DeviceState, ReachabilitySource

from .conftest import (
    make_online_api_device,
    make_state_monitor_with_callbacks,
    stub_async_service_info,
)

_SERVICE_NAME = "kitchen._esphomelib._tcp.local."


def _prime_sweep(monitor: Any, *, cache_trace: bool = True, live_ptr: bool = False) -> None:
    """Wire the fake zeroconf plus the two cache reads the sweep filter makes."""
    monitor._mdns._zeroconf = MagicMock()
    monitor.get_mdns_cache_info = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock() if cache_trace else None
    )
    monitor._mdns.has_live_ptr = MagicMock(return_value=live_ptr)  # type: ignore[method-assign]


async def test_sweep_claims_mdns_for_ping_owned_online_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ping owns the ledger, the cache resolves → mdns reclaims without touching the wire."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_sweep(monitor)
    info = stub_async_service_info(monkeypatch, cached=True)

    await shared.resolve_api_mdns_targets(monitor)

    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS
    assert ("on_source_change", "kitchen", ReachabilitySource.MDNS) in callbacks.calls
    assert device.runtime_state.deployed_version == "2026.7.0"
    info.async_request.assert_not_called()


async def test_sweep_cache_miss_falls_back_to_wire_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire fallback claims on an answer, bounded by the sweep-scoped timeout."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_sweep(monitor)
    info = stub_async_service_info(monkeypatch, resolved=True)

    await shared.resolve_api_mdns_targets(monitor)

    info.async_request.assert_awaited_once()
    assert info.async_request.await_args.kwargs["timeout"] == mdns_module._SWEEP_RESOLVE_TIMEOUT_MS
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS


async def test_sweep_resolve_miss_claims_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A miss leaves the ledger and state alone — ICMP decides, never the resolve."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_sweep(monitor)
    stub_async_service_info(monkeypatch)

    await shared.resolve_api_mdns_targets(monitor)

    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING
    assert callbacks.calls_for("on_state_change") == []


@pytest.mark.parametrize(
    ("overrides", "source", "prime"),
    [
        pytest.param({"state": DeviceState.OFFLINE}, ReachabilitySource.PING, {}, id="offline"),
        pytest.param({"state": DeviceState.UNKNOWN}, ReachabilitySource.PING, {}, id="unknown"),
        pytest.param(
            {"api_enabled": False, "loaded_integrations": ["mqtt", "wifi"]},
            ReachabilitySource.PING,
            {},
            id="non_api",
        ),
        pytest.param({}, ReachabilitySource.MQTT, {}, id="mqtt_owned"),
        pytest.param({}, ReachabilitySource.PING, {"cache_trace": False}, id="no_cache_trace"),
        pytest.param({}, ReachabilitySource.MDNS, {"live_ptr": True}, id="mdns_owned_live_ptr"),
    ],
)
async def test_sweep_skips_ineligible_devices(
    overrides: dict[str, Any], source: ReachabilitySource, prime: dict[str, bool]
) -> None:
    """Not-online, non-API, mqtt-owned, traceless, and browser-owned devices are never resolved."""
    device = make_online_api_device(**overrides)
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = source
    _prime_sweep(monitor, **prime)
    monitor._mdns.resolve_and_claim = AsyncMock()  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)

    monitor._mdns.resolve_and_claim.assert_not_called()


async def test_sweep_resolves_multiple_candidates_concurrently() -> None:
    devices = [make_online_api_device("kitchen"), make_online_api_device("porch")]
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    monitor.state.state_source["porch"] = ReachabilitySource.PING
    _prime_sweep(monitor)
    resolve = AsyncMock()
    monitor._mdns.resolve_and_claim = resolve  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)

    assert {call.args[0] for call in resolve.await_args_list} == {"kitchen", "porch"}


async def test_sweep_surfaces_unexpected_resolve_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising ``resolve_and_claim`` is logged as a bug, not masked as a benign miss."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _prime_sweep(monitor)
    monitor._mdns.resolve_and_claim = AsyncMock(  # type: ignore[method-assign]
        side_effect=AttributeError("boom")
    )

    with caplog.at_level(logging.WARNING):
        await shared.resolve_api_mdns_targets(monitor)

    assert "Resolve-first mDNS claim for kitchen raised unexpectedly" in caplog.text


async def test_sweep_without_zeroconf_is_a_noop() -> None:
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    monitor._mdns._zeroconf = None
    monitor._mdns.resolve_and_claim = AsyncMock()  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)

    monitor._mdns.resolve_and_claim.assert_not_called()


async def test_sweep_rechecks_mdns_owned_device_without_live_ptr() -> None:
    """An mdns claim with no live PTR keeps sweep eligibility until the PTR returns."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    _prime_sweep(monitor, live_ptr=False)
    resolve = AsyncMock()
    monitor._mdns.resolve_and_claim = resolve  # type: ignore[method-assign]

    await shared.resolve_api_mdns_targets(monitor)

    resolve.assert_awaited_once_with("kitchen")


async def test_resolve_and_claim_without_zeroconf_is_a_noop() -> None:
    monitor, callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    monitor._mdns._zeroconf = None

    await monitor._mdns.resolve_and_claim("kitchen")

    assert callbacks.calls == []


def test_should_ping_gates_mdns_ownership_on_live_ptr() -> None:
    """An mdns claim with no live PTR has no ``Removed`` counterpart — keep sweeping it."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS

    monitor._mdns.has_live_ptr = MagicMock(return_value=False)  # type: ignore[method-assign]
    assert shared.should_ping(monitor, device) is True

    monitor._mdns.has_live_ptr = MagicMock(return_value=True)  # type: ignore[method-assign]
    assert shared.should_ping(monitor, device) is False


def test_should_ping_non_api_mdns_ownership_unchanged() -> None:
    """Non-API devices never publish a PTR; their active-resolve claim keeps the lockout."""
    device = make_online_api_device(api_enabled=False, loaded_integrations=["mqtt", "wifi"])
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    monitor._mdns.has_live_ptr = MagicMock(return_value=False)  # type: ignore[method-assign]

    assert shared.should_ping(monitor, device) is False


def test_has_live_ptr_reads_the_browser_cache() -> None:
    monitor, _callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    fake_zeroconf = MagicMock()
    monitor._mdns._zeroconf = fake_zeroconf
    lookup = fake_zeroconf.zeroconf.cache.current_entry_with_name_and_alias

    ptr = MagicMock()
    ptr.is_expired.return_value = False
    lookup.return_value = ptr
    assert monitor._mdns.has_live_ptr("kitchen") is True
    lookup.assert_called_with("_esphomelib._tcp.local.", _SERVICE_NAME)

    ptr.is_expired.return_value = True
    assert monitor._mdns.has_live_ptr("kitchen") is False

    lookup.return_value = None
    assert monitor._mdns.has_live_ptr("kitchen") is False

    monitor._mdns._zeroconf = None
    assert monitor._mdns.has_live_ptr("kitchen") is False


def _gated_wire(info: MagicMock, *, result: bool) -> tuple[asyncio.Event, list[int]]:
    """Hold the stub's wire resolve open until the returned event is set."""
    gate = asyncio.Event()
    calls: list[int] = []

    async def _wire(*_args: Any, **_kwargs: Any) -> bool:
        calls.append(1)
        await gate.wait()
        return result

    info.async_request = _wire
    return gate, calls


async def test_added_during_removed_verify_keeps_device_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device returning mid-verify (cache-hit ``Added``) stays ONLINE through both paths."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    info = stub_async_service_info(monkeypatch, cached=True)
    gate, _calls = _gated_wire(info, result=True)

    verify = asyncio.create_task(
        monitor._mdns._verify_removed(MagicMock(), _SERVICE_NAME, "kitchen")
    )
    await asyncio.sleep(0)
    assert _SERVICE_NAME in monitor._mdns._inflight_resolves

    monitor._mdns._on_esphomelib_service_state_change(
        MagicMock(), "_esphomelib._tcp.local.", _SERVICE_NAME, mdns_module.ServiceStateChange.Added
    )
    assert device.runtime_state.state == DeviceState.ONLINE

    gate.set()
    await verify
    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS
    assert callbacks.calls_for("on_state_change") == []


async def test_added_resolve_during_verify_defers_to_the_inflight_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-miss ``Added`` mid-verify spawns no second wire resolve; the verify decides."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    info = stub_async_service_info(monkeypatch)
    gate, calls = _gated_wire(info, result=True)

    verify = asyncio.create_task(
        monitor._mdns._verify_removed(MagicMock(), _SERVICE_NAME, "kitchen")
    )
    await asyncio.sleep(0)

    monitor._mdns._on_esphomelib_service_state_change(
        MagicMock(), "_esphomelib._tcp.local.", _SERVICE_NAME, mdns_module.ServiceStateChange.Added
    )
    gate.set()
    await verify
    while monitor._tasks:
        await asyncio.gather(*list(monitor._tasks), return_exceptions=True)

    assert len(calls) == 1
    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.MDNS


async def test_confirmed_wire_miss_outranks_a_stale_cache_added_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-hit ``Added`` mid-verify can't save a device the wire says is gone."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    info = stub_async_service_info(monkeypatch, cached=True)
    gate, _calls = _gated_wire(info, result=False)

    verify = asyncio.create_task(
        monitor._mdns._verify_removed(MagicMock(), _SERVICE_NAME, "kitchen")
    )
    await asyncio.sleep(0)

    monitor._mdns._on_esphomelib_service_state_change(
        MagicMock(), "_esphomelib._tcp.local.", _SERVICE_NAME, mdns_module.ServiceStateChange.Added
    )
    assert device.runtime_state.state == DeviceState.ONLINE

    gate.set()
    await verify
    assert device.runtime_state.state == DeviceState.OFFLINE
    assert "kitchen" not in monitor.state.state_source


async def test_verify_removed_keeps_online_on_a_swallowed_resolve_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An internal resolve error is not a confirmed miss — never demote on uncertainty."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    info = stub_async_service_info(monkeypatch)
    info.async_request.side_effect = OSError("socket gone")

    with caplog.at_level(logging.WARNING):
        await monitor._mdns._verify_removed(MagicMock(), _SERVICE_NAME, "kitchen")

    assert device.runtime_state.state == DeviceState.ONLINE
    assert callbacks.calls_for("on_state_change") == []
    assert "Removed-verify resolve for kitchen errored" in caplog.text


async def test_verify_removed_bails_when_a_resolve_is_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent resolve decides — the removed-verify must not demote without verifying."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    monitor._mdns._inflight_resolves.add(_SERVICE_NAME)
    info = stub_async_service_info(monkeypatch)

    await monitor._mdns._verify_removed(MagicMock(), _SERVICE_NAME, "kitchen")

    info.async_request.assert_not_called()
    assert device.runtime_state.state == DeviceState.ONLINE
    assert callbacks.calls_for("on_state_change") == []
