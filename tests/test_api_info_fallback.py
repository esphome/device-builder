"""Tests for the Native API mac/version fallback source.

When mDNS multicast can't reach the dashboard, an online API device has a
blank ``mac_address`` / ``deployed_version`` because those come only from
the ``_esphomelib._tcp`` TXT records. :class:`ApiInfoSource` connects over
the Native API in a subprocess to fill them. These tests pin the self-gate
(only online API devices missing a field, with a routable address), the
apply path, the failure cooldown, and the noise-PSK plumbing — the worker
subprocess itself is stubbed via ``_run_worker``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.controllers._device_state_monitor import api_info as api_info_module
from esphome_device_builder.controllers._device_state_monitor.api_info import ApiInfoSource
from esphome_device_builder.helpers import api_device_info
from esphome_device_builder.helpers.subprocess import CapturedSubprocess
from esphome_device_builder.models import Device, DeviceState, ReachabilitySource

from .conftest import make_device, make_online_api_device, make_state_monitor_with_callbacks

# ----------------------------------------------------------------------
# Self-gating — _select_targets
# ----------------------------------------------------------------------


def test_select_targets_picks_online_api_device_missing_fields() -> None:
    """The target case: ONLINE, API-capable, blank mac+version, routable IP."""
    devices = [make_online_api_device()]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    assert [d.name for d in monitor._api_info._select_targets()] == ["kitchen"]


def test_select_targets_picks_when_only_one_field_missing() -> None:
    """A device with a MAC but no version still needs a fetch."""
    devices = [make_online_api_device(mac_address="94:C9:60:1F:8C:F1")]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    assert [d.name for d in monitor._api_info._select_targets()] == ["kitchen"]


def test_select_targets_skips_when_both_fields_present() -> None:
    """MDNS already supplied both → never connect."""
    devices = [make_online_api_device(mac_address="94:C9:60:1F:8C:F1", deployed_version="2026.6.1")]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    assert monitor._api_info._select_targets() == []


def test_select_targets_skips_offline_device() -> None:
    """Only ONLINE devices are probed."""
    devices = [make_online_api_device(state=DeviceState.OFFLINE)]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    assert monitor._api_info._select_targets() == []


def test_select_targets_skips_non_api_device() -> None:
    """A device that exposes no Native API can't be reached over it."""
    devices = [make_online_api_device(api_enabled=False, loaded_integrations=["web_server"])]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    assert monitor._api_info._select_targets() == []


def test_select_targets_picks_uncompiled_online_api_device() -> None:
    """An online ``api:`` device never compiled here (empty loaded_integrations) is still probed."""
    devices = [make_online_api_device(loaded_integrations=[])]  # api_enabled set from YAML scan
    monitor, _ = make_state_monitor_with_callbacks(devices)
    assert [d.name for d in monitor._api_info._select_targets()] == ["kitchen"]


def test_select_targets_skips_when_only_local_hostname_known() -> None:
    """No IP and only a ``.local`` address → unresolvable when mDNS is down."""
    devices = [make_online_api_device(ip="", ip_addresses=[], address="kitchen.local")]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    assert monitor._api_info._select_targets() == []


def test_select_targets_picks_mdns_owned_device_missing_fields() -> None:
    """MDNS ownership proves a resolve, not an applied TXT — a blank device is still due."""
    devices = [make_online_api_device()]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    assert [d.name for d in monitor._api_info._select_targets()] == ["kitchen"]


def test_select_targets_skips_forced_reprobe_when_mdns_owns_state() -> None:
    """A forced re-probe of a fully-populated device defers to a live mDNS announce."""
    devices = [make_online_api_device(mac_address="94:C9:60:1F:8C:F1", deployed_version="2026.6.1")]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    monitor.state.state_source["kitchen"] = ReachabilitySource.MDNS
    monitor._api_info.request_reprobe("kitchen")
    assert monitor._api_info._select_targets() == []


def test_select_targets_picks_forced_reprobe_when_ping_owned() -> None:
    """A forced re-probe runs when mDNS isn't reaching the device."""
    devices = [make_online_api_device(mac_address="94:C9:60:1F:8C:F1", deployed_version="2026.6.1")]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    monitor._api_info.request_reprobe("kitchen")
    assert [d.name for d in monitor._api_info._select_targets()] == ["kitchen"]


def test_select_targets_picks_when_online_via_ping_not_mdns() -> None:
    """ONLINE via ping but not mDNS → mDNS is broken for this device; probe it."""
    devices = [make_online_api_device()]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    assert [d.name for d in monitor._api_info._select_targets()] == ["kitchen"]


# ----------------------------------------------------------------------
# _candidate_addresses
# ----------------------------------------------------------------------


def test_candidate_addresses_prefers_full_address_list() -> None:
    device = make_online_api_device(ip="192.168.1.50", ip_addresses=["192.168.1.50", "fe80::1"])
    assert ApiInfoSource._candidate_addresses(device) == ["192.168.1.50", "fe80::1"]


def test_candidate_addresses_falls_back_to_single_ip() -> None:
    device = make_online_api_device(ip="192.168.1.50", ip_addresses=[])
    assert ApiInfoSource._candidate_addresses(device) == ["192.168.1.50"]


def test_candidate_addresses_accepts_non_local_hostname() -> None:
    device = make_online_api_device(ip="", ip_addresses=[], address="device.example.com")
    assert ApiInfoSource._candidate_addresses(device) == ["device.example.com"]


def test_candidate_addresses_rejects_local_hostname() -> None:
    device = make_online_api_device(ip="", ip_addresses=[], address="kitchen.local")
    assert ApiInfoSource._candidate_addresses(device) == []


# ----------------------------------------------------------------------
# _fetch — apply, cooldown, noise PSK
# ----------------------------------------------------------------------


async def test_fetch_applies_mac_and_version() -> None:
    """A worker hit writes the normalized MAC and the version through the monitor."""
    device = make_online_api_device(address="")
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor._api_info._run_worker = AsyncMock(  # type: ignore[method-assign]
        return_value={"mac_address": "94c9601f8cf1", "esphome_version": "2026.6.1"}
    )

    await monitor._api_info._fetch(device)

    assert device.mac_address == "94:C9:60:1F:8C:F1"
    assert device.deployed_version == "2026.6.1"
    assert ("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1") in callbacks.calls
    assert ("on_version_change", "kitchen", "2026.6.1") in callbacks.calls


async def test_fetch_failure_sets_cooldown_and_skips_next_select() -> None:
    """A failed fetch parks the device so the next sweep doesn't reconnect it."""
    device = make_online_api_device()
    monitor, _ = make_state_monitor_with_callbacks([device])
    monitor._api_info._run_worker = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert [d.name for d in monitor._api_info._select_targets()] == ["kitchen"]
    await monitor._api_info._fetch(device)
    assert monitor._api_info._select_targets() == []


async def test_fetch_passes_resolved_key_and_port_to_worker() -> None:
    """An encrypted device's key and configured port reach the worker request."""
    device = make_online_api_device(address="")
    monitor = DeviceStateMonitor(
        get_devices=lambda: [device],
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
        resolve_api_connection=AsyncMock(return_value=("s3cr3t-psk", 6055)),
    )
    monitor._api_info._run_worker = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await monitor._api_info._fetch(device)

    request = json.loads(monitor._api_info._run_worker.call_args.args[1])
    assert request["noise_psk"] == "s3cr3t-psk"
    assert request["port"] == 6055
    assert request["address"] == "192.168.1.50"
    assert request["addresses"] == ["192.168.1.50"]


async def test_fetch_uses_plaintext_default_port_when_no_resolver() -> None:
    """Unwired resolver → empty PSK (plaintext) and the default 6053 port."""
    device = make_online_api_device(address="")
    monitor, _ = make_state_monitor_with_callbacks([device])
    monitor._api_info._run_worker = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await monitor._api_info._fetch(device)

    request = json.loads(monitor._api_info._run_worker.call_args.args[1])
    assert request["noise_psk"] == ""
    assert request["port"] == 6053


async def test_fetch_connected_but_empty_sets_cooldown() -> None:
    """A probe that connects but returns no data still backs off (no every-sweep reconnect)."""
    device = make_online_api_device()
    monitor, _ = make_state_monitor_with_callbacks([device])
    monitor._api_info._run_worker = AsyncMock(  # type: ignore[method-assign]
        return_value={"mac_address": "", "esphome_version": ""}
    )

    await monitor._api_info._fetch(device)

    assert monitor._api_info._select_targets() == []


async def test_fetch_partial_fill_is_progress_not_failure() -> None:
    """A probe that fills only the MAC isn't cooled down and stays eligible for the rest."""
    device = make_online_api_device(address="")
    monitor, _ = make_state_monitor_with_callbacks([device])
    src = monitor._api_info
    src._run_worker = AsyncMock(  # type: ignore[method-assign]
        return_value={"mac_address": "94c9601f8cf1", "esphome_version": ""}
    )

    await src._fetch(device)

    assert device.mac_address == "94:C9:60:1F:8C:F1"
    assert device.deployed_version == ""  # version still missing
    assert "kitchen" not in src._cooldown  # not cooled down → normal-interval retry
    assert [d.name for d in src._select_targets()] == ["kitchen"]  # still chasing version


async def test_fetch_no_new_fill_is_a_failure() -> None:
    """Re-sending an already-known MAC with no version is a miss (cooldown)."""
    device = make_online_api_device(mac_address="94:C9:60:1F:8C:F1", address="")
    monitor, _ = make_state_monitor_with_callbacks([device])
    src = monitor._api_info
    src._run_worker = AsyncMock(  # type: ignore[method-assign]
        return_value={"mac_address": "94c9601f8cf1", "esphome_version": ""}
    )

    await src._fetch(device)

    assert "kitchen" in src._cooldown


async def test_fetch_resolver_failure_is_a_recorded_miss() -> None:
    """A resolver exception records a miss and skips the doomed plaintext probe."""
    device = make_online_api_device(address="")
    monitor = DeviceStateMonitor(
        get_devices=lambda: [device],
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
        resolve_api_connection=AsyncMock(side_effect=RuntimeError("resolve boom")),
    )
    monitor._api_info._run_worker = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await monitor._api_info._fetch(device)

    monitor._api_info._run_worker.assert_not_called()
    assert "kitchen" in monitor._api_info._cooldown


async def test_fetch_skips_encrypted_device_without_key() -> None:
    """A declared-encrypted device with no resolvable key is a recorded miss, not a probe."""
    device = make_online_api_device(api_encrypted=True, address="")
    monitor = DeviceStateMonitor(
        get_devices=lambda: [device],
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
        resolve_api_connection=AsyncMock(return_value=("", 6053)),  # encrypted, key empty
    )
    monitor._api_info._run_worker = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await monitor._api_info._fetch(device)

    monitor._api_info._run_worker.assert_not_called()
    assert "kitchen" in monitor._api_info._cooldown


async def test_fetch_skips_when_addresses_emptied_after_select() -> None:
    """A select→fetch TOCTOU (no addresses left) is a recorded miss, not an IndexError."""
    device = make_online_api_device(ip="", ip_addresses=[], address="kitchen.local")
    monitor, _ = make_state_monitor_with_callbacks([device])
    monitor._api_info._run_worker = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await monitor._api_info._fetch(device)  # must not raise IndexError

    monitor._api_info._run_worker.assert_not_called()
    assert "kitchen" in monitor._api_info._cooldown


async def test_systemic_warning_fires_once_when_many_devices_failing(
    caplog: Any, monkeypatch: Any
) -> None:
    """A sweep that leaves >= threshold distinct devices on cooldown logs one WARNING."""
    monkeypatch.setattr(api_info_module, "_MAX_PROBES_PER_SWEEP", 20)  # probe all in one sweep
    devices = [make_online_api_device(f"dev{i}") for i in range(10)]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    src = monitor._api_info
    src._run_worker = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        await src._sweep()

    systemic = [r for r in caplog.records if "is failing for" in r.getMessage()]
    assert len(systemic) == 1
    assert systemic[0].levelno == logging.WARNING


async def test_systemic_warning_not_masked_by_one_healthy_device(
    caplog: Any, monkeypatch: Any
) -> None:
    """A healthy probe among broken ones doesn't suppress the WARNING (counts distinct failures)."""
    monkeypatch.setattr(api_info_module, "_MAX_PROBES_PER_SWEEP", 20)
    devices = [make_online_api_device(f"bad{i}") for i in range(10)]
    healthy = make_online_api_device("good")
    monitor, _ = make_state_monitor_with_callbacks([*devices, healthy])
    src = monitor._api_info

    async def _run_worker(device: Device, _request: bytes) -> dict[str, str] | None:
        if device.name == "good":
            return {"mac_address": "94c9601f8cf1", "esphome_version": "2026.6.1"}
        return None

    src._run_worker = _run_worker  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        await src._sweep()

    systemic = [r for r in caplog.records if "is failing for" in r.getMessage()]
    assert len(systemic) == 1  # 10 broken devices still trip it despite 'good' succeeding


async def test_systemic_warning_rearms_after_recovery(monkeypatch: Any) -> None:
    """When the failing-device count drops below threshold, the WARNING re-arms."""
    monkeypatch.setattr(api_info_module, "_MAX_PROBES_PER_SWEEP", 20)
    devices = [make_online_api_device(f"dev{i}") for i in range(10)]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    src = monitor._api_info
    src._run_worker = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await src._sweep()
    assert src._warned_systemic is True

    # Devices recover (mDNS fills both) → no longer due → failing count drops.
    for device in devices:
        device.mac_address = "94:C9:60:1F:8C:F1"
        device.deployed_version = "2026.6.1"
    src._evaluate_systemic_health()
    assert src._warned_systemic is False


async def test_sweep_prunes_cooldown_for_removed_devices() -> None:
    """A cooldown entry for a device no longer in the catalog is dropped."""
    device = make_online_api_device()
    monitor, _ = make_state_monitor_with_callbacks([device])
    src = monitor._api_info
    src._cooldown = {"ghost": 1e18, "kitchen": 1e18}
    src._fetch = AsyncMock()  # type: ignore[method-assign]

    await src._sweep()

    assert "ghost" not in src._cooldown
    assert "kitchen" in src._cooldown


# ----------------------------------------------------------------------
# run() — dependency gate
# ----------------------------------------------------------------------


async def test_run_without_aioesphomeapi_still_sweeps(monkeypatch: Any) -> None:
    """No aioesphomeapi installed → the loop still runs (the cache reconcile needs no worker)."""
    monitor, _ = make_state_monitor_with_callbacks([make_online_api_device()])
    src = monitor._api_info
    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.api_info.importlib.util.find_spec",
        lambda _name: None,
    )
    monkeypatch.setattr(api_info_module, "_BOOTSTRAP_DELAY", 0)
    sweep = AsyncMock()

    async def _idle() -> None:
        raise asyncio.CancelledError

    src._sweep = sweep  # type: ignore[method-assign]
    src._idle = _idle  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await src.run()

    sweep.assert_called_once()
    assert src._api_available is False


async def test_sweep_without_aioesphomeapi_reconciles_but_never_connects() -> None:
    """The API-connect stage is gated on aioesphomeapi; the cache reconcile is not."""
    monitor, _ = make_state_monitor_with_callbacks([make_online_api_device()])
    src = monitor._api_info
    src._api_available = False
    reconciled: list[str] = []
    monitor.reconcile_from_mdns_cache = reconciled.append  # type: ignore[method-assign]
    src._fetch = AsyncMock()  # type: ignore[method-assign]

    await src._sweep()

    assert reconciled == ["kitchen"]
    src._fetch.assert_not_called()


async def test_run_sweeps_then_idles(monkeypatch: Any) -> None:
    """With aioesphomeapi present the loop bootstraps, sweeps, then idles each cycle."""
    monitor, _ = make_state_monitor_with_callbacks([])
    src = monitor._api_info
    monkeypatch.setattr(api_info_module, "_BOOTSTRAP_DELAY", 0)
    swept: list[int] = []

    async def _sweep() -> None:
        swept.append(1)

    async def _idle() -> None:
        raise asyncio.CancelledError

    src._sweep = _sweep  # type: ignore[method-assign]
    src._idle = _idle  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await src.run()
    assert swept == [1]


async def test_run_survives_a_sweep_error(monkeypatch: Any) -> None:
    """An unexpected error from a sweep is logged and the loop keeps going."""
    monitor, _ = make_state_monitor_with_callbacks([])
    src = monitor._api_info
    monkeypatch.setattr(api_info_module, "_BOOTSTRAP_DELAY", 0)
    reached_idle: list[int] = []

    async def _boom_sweep() -> None:
        raise RuntimeError("sweep blew up")

    async def _idle() -> None:
        reached_idle.append(1)
        raise asyncio.CancelledError

    src._sweep = _boom_sweep  # type: ignore[method-assign]
    src._idle = _idle  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await src.run()
    assert reached_idle == [1]  # the sweep error didn't escape run(); we reached idle


class _FakePresence:
    """Minimal SubscriberPresence stand-in: records wake callbacks, never blocks."""

    def __init__(self) -> None:
        self.callbacks: list[Any] = []

    def add_subscriber_callback(self, callback: Any) -> None:
        self.callbacks.append(callback)

    async def wait_for_subscriber(self) -> None:
        return None


async def test_run_waits_for_subscriber_when_presence_wired(monkeypatch: Any) -> None:
    """With a presence gate, the loop registers a wake callback and waits per cycle."""
    presence = _FakePresence()
    monitor = DeviceStateMonitor(
        get_devices=lambda: [make_online_api_device()],
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
        presence=presence,
    )
    assert presence.callbacks  # ApiInfoSource registered its wake on construction
    src = monitor._api_info
    monkeypatch.setattr(api_info_module, "_BOOTSTRAP_DELAY", 0)
    swept: list[int] = []

    async def _sweep() -> None:
        swept.append(1)

    async def _idle() -> None:
        raise asyncio.CancelledError

    src._sweep = _sweep  # type: ignore[method-assign]
    src._idle = _idle  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await src.run()
    assert swept == [1]


async def test_idle_returns_immediately_when_woken() -> None:
    """A set wake event short-circuits the idle wait."""
    monitor, _ = make_state_monitor_with_callbacks([])
    monitor._api_info._wake.set()
    await monitor._api_info._idle()


async def test_idle_times_out_when_not_woken(monkeypatch: Any) -> None:
    """With no wake, the idle wait expires after the interval and returns."""
    monkeypatch.setattr(api_info_module, "_INTERVAL", 0.01)
    monitor, _ = make_state_monitor_with_callbacks([])
    await monitor._api_info._idle()


# ----------------------------------------------------------------------
# _sweep — serial, one probe at a time
# ----------------------------------------------------------------------


async def test_sweep_fetches_each_selected_target() -> None:
    """Every selected device is probed, one at a time."""
    monitor, _ = make_state_monitor_with_callbacks(
        [make_online_api_device("alpha"), make_online_api_device("beta")]
    )
    fetched: list[str] = []

    async def _fetch(device: Device) -> None:
        fetched.append(device.name)

    monitor._api_info._fetch = _fetch  # type: ignore[method-assign]
    await monitor._api_info._sweep()
    assert sorted(fetched) == ["alpha", "beta"]


async def test_sweep_noop_when_no_targets() -> None:
    """An empty target set spawns nothing."""
    monitor, _ = make_state_monitor_with_callbacks([])
    monitor._api_info._fetch = AsyncMock()  # type: ignore[method-assign]
    await monitor._api_info._sweep()
    monitor._api_info._fetch.assert_not_called()


async def test_sweep_skips_api_probe_when_cache_reconcile_fills_fields() -> None:
    """A device the zeroconf cache repairs drops out before the API-connect stage."""
    device = make_online_api_device()
    monitor, _ = make_state_monitor_with_callbacks([device])

    def _fill(_name: str) -> None:
        device.mac_address = "94:C9:60:1F:8C:F1"
        device.deployed_version = "2026.6.4"
        device.deployed_config_hash = "abcd1234"

    monitor.reconcile_from_mdns_cache = _fill  # type: ignore[method-assign]
    monitor._api_info._fetch = AsyncMock()  # type: ignore[method-assign]

    await monitor._api_info._sweep()

    monitor._api_info._fetch.assert_not_called()


async def test_sweep_probes_when_cache_reconcile_cannot_fill() -> None:
    """A cache miss leaves the device due; the API-connect stage still runs."""
    monitor, _ = make_state_monitor_with_callbacks([make_online_api_device()])
    reconciled: list[str] = []
    monitor.reconcile_from_mdns_cache = reconciled.append  # type: ignore[method-assign]
    fetch = AsyncMock()
    monitor._api_info._fetch = fetch  # type: ignore[method-assign]

    await monitor._api_info._sweep()

    assert reconciled == ["kitchen"]
    fetch.assert_called_once()


async def test_sweep_reconciles_only_blank_online_api_devices() -> None:
    """Fully-populated, offline, and non-API devices skip the reconcile pass."""
    populated = make_online_api_device(
        "full",
        mac_address="94:C9:60:1F:8C:F1",
        deployed_version="2026.6.4",
        deployed_config_hash="abcd1234",
        api_encryption_active="",
    )
    offline = make_online_api_device("dark", state=DeviceState.OFFLINE)
    no_api = make_online_api_device("web", api_enabled=False, loaded_integrations=["web_server"])
    blank = make_online_api_device("blank")
    monitor, _ = make_state_monitor_with_callbacks([populated, offline, no_api, blank])
    reconciled: list[str] = []
    monitor.reconcile_from_mdns_cache = reconciled.append  # type: ignore[method-assign]
    monitor._api_info._fetch = AsyncMock()  # type: ignore[method-assign]

    await monitor._api_info._sweep()

    assert reconciled == ["blank"]


async def test_sweep_reconciles_unknown_encryption_state_even_when_fields_full() -> None:
    """mac+version+hash present but encryption never observed → reconcile runs, no API probe."""
    device = make_online_api_device(
        mac_address="94:C9:60:1F:8C:F1",
        deployed_version="2026.6.4",
        deployed_config_hash="abcd1234",
    )
    assert device.api_encryption_active is None
    monitor, _ = make_state_monitor_with_callbacks([device])
    reconciled: list[str] = []
    monitor.reconcile_from_mdns_cache = reconciled.append  # type: ignore[method-assign]
    fetch = AsyncMock()
    monitor._api_info._fetch = fetch  # type: ignore[method-assign]

    await monitor._api_info._sweep()

    assert reconciled == ["kitchen"]
    fetch.assert_not_called()


async def test_sweep_reconciles_missing_config_hash_even_when_not_api_due() -> None:
    """mac+version present but no config_hash → cache reconcile runs, API probe doesn't."""
    device = make_online_api_device(
        mac_address="94:C9:60:1F:8C:F1", deployed_version="2026.6.4", deployed_config_hash=""
    )
    monitor, _ = make_state_monitor_with_callbacks([device])
    reconciled: list[str] = []
    monitor.reconcile_from_mdns_cache = reconciled.append  # type: ignore[method-assign]
    fetch = AsyncMock()
    monitor._api_info._fetch = fetch  # type: ignore[method-assign]

    await monitor._api_info._sweep()

    assert reconciled == ["kitchen"]
    fetch.assert_not_called()


async def test_sweep_caps_probes_per_sweep(monkeypatch: Any) -> None:
    """A large due fleet is probed in bounded batches; the overflow rolls to the next sweep."""
    monkeypatch.setattr(api_info_module, "_MAX_PROBES_PER_SWEEP", 3)
    devices = [make_online_api_device(f"dev{i}") for i in range(7)]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    fetched: list[str] = []

    async def _fetch(device: Device) -> None:
        fetched.append(device.name)

    monitor._api_info._fetch = _fetch  # type: ignore[method-assign]
    await monitor._api_info._sweep()
    assert len(fetched) == 3


async def test_sweep_isolates_a_failing_fetch() -> None:
    """One device whose fetch raises is cooled down; the sweep finishes the rest."""
    a, b = make_online_api_device("a"), make_online_api_device("b")
    monitor, _ = make_state_monitor_with_callbacks([a, b])
    src = monitor._api_info
    fetched: list[str] = []

    async def _fetch(device: Device) -> None:
        fetched.append(device.name)
        if device.name == "a":
            raise IndexError("addresses emptied between select and fetch")

    src._fetch = _fetch  # type: ignore[method-assign]
    await src._sweep()  # must not raise

    assert sorted(fetched) == ["a", "b"]  # b still probed after a blew up
    assert "a" in src._cooldown  # the bad device got backed off


async def test_sweep_exceptions_cool_down_and_count_as_failing(
    caplog: Any, monkeypatch: Any
) -> None:
    """An unexpected per-device error logs at WARNING, cools the device down, and counts."""
    monkeypatch.setattr(api_info_module, "_MAX_PROBES_PER_SWEEP", 20)
    devices = [make_online_api_device(f"dev{i}") for i in range(10)]
    monitor, _ = make_state_monitor_with_callbacks(devices)
    src = monitor._api_info

    async def _boom(_device: Device) -> None:
        raise RuntimeError("kaboom")

    src._fetch = _boom  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        await src._sweep()

    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)
    systemic = [r for r in caplog.records if "is failing for" in r.getMessage()]
    assert len(systemic) == 1  # 10 cooled-down devices trip the systemic WARNING


# ----------------------------------------------------------------------
# _run_worker — result handling on top of run_subprocess_capture
# ----------------------------------------------------------------------


def _patch_capture(
    monkeypatch: Any,
    *,
    returncode: int | None = 0,
    stdout: bytes = b"",
    timed_out: bool = False,
    error: BaseException | None = None,
) -> AsyncMock:
    """Stub ``run_subprocess_capture`` with a fake result (or raise *error*)."""
    if error is not None:
        mock = AsyncMock(side_effect=error)
    else:
        mock = AsyncMock(
            return_value=CapturedSubprocess(
                returncode=returncode, stdout=stdout, timed_out=timed_out
            )
        )
    monkeypatch.setattr(api_info_module, "run_subprocess_capture", mock)
    return mock


async def test_run_worker_parses_json_payload(monkeypatch: Any) -> None:
    monitor, _ = make_state_monitor_with_callbacks([])
    _patch_capture(monkeypatch, stdout=b'{"mac_address": "aa", "esphome_version": "1"}')
    result = await monitor._api_info._run_worker(make_device(), b"{}")
    assert result == {"mac_address": "aa", "esphome_version": "1"}


async def test_run_worker_feeds_request_over_stdin_with_stderr_discarded(monkeypatch: Any) -> None:
    """The request goes to the child as stdin and stderr is dropped (clean stdout)."""
    monitor, _ = make_state_monitor_with_callbacks([])
    mock = _patch_capture(monkeypatch, stdout=b'{"mac_address": "aa", "esphome_version": "1"}')
    await monitor._api_info._run_worker(make_device(), b"REQUEST-BYTES")
    _, kwargs = mock.call_args
    assert kwargs["stdin_data"] == b"REQUEST-BYTES"
    assert kwargs["merge_stderr"] is False


@pytest.mark.parametrize(
    "capture_kwargs",
    [
        pytest.param({"returncode": 1, "stdout": b"{}"}, id="nonzero_exit"),
        pytest.param({"stdout": b""}, id="empty_stdout"),
        pytest.param({"returncode": None, "timed_out": True}, id="timeout"),
        pytest.param({"stdout": b"not json"}, id="unparsable_stdout"),
        pytest.param({"error": OSError("cannot exec")}, id="spawn_oserror"),
    ],
)
async def test_run_worker_returns_none(monkeypatch: Any, capture_kwargs: dict[str, Any]) -> None:
    """Every non-success worker outcome maps to a missed probe (``None``)."""
    monitor, _ = make_state_monitor_with_callbacks([])
    _patch_capture(monkeypatch, **capture_kwargs)
    assert await monitor._api_info._run_worker(make_device(), b"{}") is None


async def test_run_worker_propagates_cancellation(monkeypatch: Any) -> None:
    """Cancellation from the capture helper is re-raised, not turned into a miss."""
    monitor, _ = make_state_monitor_with_callbacks([])
    _patch_capture(monkeypatch, error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await monitor._api_info._run_worker(make_device(), b"{}")


async def test_run_worker_logs_worker_reported_error(monkeypatch: Any, caplog: Any) -> None:
    """The worker's structured ``{"error": ...}`` reason is surfaced at debug."""
    monitor, _ = make_state_monitor_with_callbacks([])
    _patch_capture(
        monkeypatch, returncode=1, stdout=b'{"error": "APIConnectionError: connection refused"}'
    )
    with caplog.at_level(logging.DEBUG):
        assert await monitor._api_info._run_worker(make_device(), b"{}") is None
    assert "connection refused" in caplog.text


# ----------------------------------------------------------------------
# Subprocess worker module (esphome_device_builder.helpers.api_device_info)
# ----------------------------------------------------------------------


def _fake_api_client(
    info: object | None = None, connect_error: Exception | None = None
) -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock(side_effect=connect_error)
    client.device_info = AsyncMock(return_value=info)
    client.disconnect = AsyncMock()
    return client


async def test_worker_fetch_returns_mac_and_version(monkeypatch: Any) -> None:
    info = SimpleNamespace(mac_address="AA:BB:CC:DD:EE:FF", esphome_version="2026.6.1")
    client = _fake_api_client(info=info)
    monkeypatch.setattr("aioesphomeapi.APIClient", MagicMock(return_value=client))

    result = await api_device_info._fetch(
        {"address": "1.2.3.4", "port": 6053, "noise_psk": "", "addresses": ["1.2.3.4"]}
    )

    assert result == {"mac_address": "AA:BB:CC:DD:EE:FF", "esphome_version": "2026.6.1"}
    client.connect.assert_awaited_once()
    client.disconnect.assert_awaited_once()


def test_worker_main_returns_2_on_bad_stdin(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert api_device_info.main() == 2
    assert "bad request" in json.loads(capsys.readouterr().out)["error"]


def test_worker_main_returns_1_and_reports_reason_on_connect_failure(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"address": "1.2.3.4", "port": 6053, "noise_psk": "", "addresses": []})
        ),
    )
    client = _fake_api_client(connect_error=OSError("unreachable"))
    monkeypatch.setattr("aioesphomeapi.APIClient", MagicMock(return_value=client))

    assert api_device_info.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert "unreachable" in payload["error"]


def test_worker_main_writes_json_on_success(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"address": "1.2.3.4", "port": 6053, "noise_psk": "k", "addresses": ["1.2.3.4"]}
            )
        ),
    )
    info = SimpleNamespace(mac_address="AA:BB:CC:DD:EE:FF", esphome_version="2026.6.1")
    monkeypatch.setattr(
        "aioesphomeapi.APIClient", MagicMock(return_value=_fake_api_client(info=info))
    )

    assert api_device_info.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "esphome_version": "2026.6.1",
    }


# ----------------------------------------------------------------------
# Done-callback — surface a crashed loop instead of dying silently
# ----------------------------------------------------------------------


def test_log_task_exit_ignores_cancelled() -> None:
    task = MagicMock()
    task.cancelled.return_value = True
    DeviceStateMonitor._log_api_info_task_exit(task)
    task.exception.assert_not_called()


def test_log_task_exit_noop_without_exception() -> None:
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = None
    DeviceStateMonitor._log_api_info_task_exit(task)  # must not raise


def test_log_task_exit_logs_crash(caplog: Any) -> None:
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("loop died")
    with caplog.at_level(logging.ERROR):
        DeviceStateMonitor._log_api_info_task_exit(task)
    assert "API info fallback loop crashed" in caplog.text


# ----------------------------------------------------------------------
# Forced re-probe — request_reprobe (post-flash version verification)
# ----------------------------------------------------------------------


def test_request_reprobe_makes_filled_device_due() -> None:
    """A forced re-probe overrides the mac+version guard that would skip the device."""
    device = make_online_api_device(mac_address="94:C9:60:1F:8C:F1", deployed_version="2026.6.1")
    monitor, _ = make_state_monitor_with_callbacks([device])
    src = monitor._api_info
    assert src._select_targets() == []  # both fields present → normally skipped
    src.request_reprobe("kitchen")
    assert [d.name for d in src._select_targets()] == ["kitchen"]


def test_request_reprobe_bypasses_cooldown() -> None:
    """A forced re-probe is deliberate — it ignores the per-device failure cooldown."""
    device = make_online_api_device()
    monitor, _ = make_state_monitor_with_callbacks([device])
    src = monitor._api_info
    src._cooldown["kitchen"] = time.monotonic() + 600
    assert src._select_targets() == []  # parked on cooldown
    src.request_reprobe("kitchen")
    assert [d.name for d in src._select_targets()] == ["kitchen"]


async def test_forced_reprobe_probes_then_clears_itself() -> None:
    """The forced probe runs even with both fields set, then the flag is consumed."""
    device = make_online_api_device(mac_address="94:C9:60:1F:8C:F1", deployed_version="2026.6.1")
    monitor, _ = make_state_monitor_with_callbacks([device])
    src = monitor._api_info
    src.request_reprobe("kitchen")
    src._run_worker = AsyncMock(  # type: ignore[method-assign]
        return_value={"esphome_version": "2026.6.2"}
    )

    await src._fetch(device)

    assert device.deployed_version == "2026.6.2"
    assert "kitchen" not in src._force_reprobe  # one-shot
    assert src._select_targets() == []  # consumed → no longer due


async def test_forced_reprobe_confirming_existing_version_is_not_a_failure() -> None:
    """A forced probe that connected and confirmed the version isn't cooled down."""
    device = make_online_api_device(mac_address="94:C9:60:1F:8C:F1", deployed_version="2026.6.2")
    monitor, _ = make_state_monitor_with_callbacks([device])
    src = monitor._api_info
    src.request_reprobe("kitchen")
    src._run_worker = AsyncMock(  # type: ignore[method-assign]
        return_value={"mac_address": "94c9601f8cf1", "esphome_version": "2026.6.2"}
    )

    await src._fetch(device)

    # Nothing newly filled, but the connect succeeded — no cooldown.
    assert "kitchen" not in src._cooldown


async def test_sweep_prunes_force_reprobe_for_dead_devices() -> None:
    """A force flag for a device that's no longer present is dropped on the next sweep."""
    device = make_online_api_device()
    monitor, _ = make_state_monitor_with_callbacks([device])
    src = monitor._api_info
    src.request_reprobe("ghost")  # not a live device
    src._fetch = AsyncMock()  # type: ignore[method-assign]

    await src._sweep()

    assert "ghost" not in src._force_reprobe


def test_monitor_request_version_reprobe_forwards_to_api_info() -> None:
    """The monitor facade forwards a version re-probe request to the API source."""
    device = make_online_api_device()
    monitor, _ = make_state_monitor_with_callbacks([device])
    monitor.request_version_reprobe("kitchen")
    assert "kitchen" in monitor._api_info._force_reprobe
