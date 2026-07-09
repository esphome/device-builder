"""Tests for the level-triggered mDNS-cache reconcile path (issue #1910)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from esphome.zeroconf import DEFAULT_TIMEOUT_MS

from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module
from esphome_device_builder.models import Device, DeviceState, ReachabilitySource

from .conftest import make_device, make_state_monitor_with_callbacks


def _device(**overrides: Any) -> Device:
    base: dict[str, Any] = {
        "state": DeviceState.ONLINE,
        "api_enabled": True,
        "loaded_integrations": ["api", "wifi"],
    }
    base.update(overrides)
    return make_device(**base)


def _seed_cache_hit(monitor: Any, monkeypatch: pytest.MonkeyPatch, props: dict[str, str]) -> None:
    """Wire a fake zeroconf whose cache resolves the kitchen service with *props*."""
    monitor._mdns._zeroconf = MagicMock()
    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.decoded_properties = props
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)


def test_reconcile_applies_txt_fields_without_claiming(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache hit fills version/config_hash/mac/encryption but never state, IP, or ownership."""
    device = _device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _seed_cache_hit(
        monitor,
        monkeypatch,
        {
            "version": "2026.6.4",
            "config_hash": "abcd1234",
            "mac": "94c9601f8cf1",
            "api_encryption": "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
        },
    )

    monitor.reconcile_from_mdns_cache("kitchen")

    assert ("on_version_change", "kitchen", "2026.6.4") in callbacks.calls
    assert ("on_config_hash_change", "kitchen", "abcd1234") in callbacks.calls
    assert ("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1") in callbacks.calls
    assert (
        "on_api_encryption_change",
        "kitchen",
        "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
    ) in callbacks.calls
    assert callbacks.calls_for("on_state_change") == []
    assert callbacks.calls_for("on_ip_change") == []
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING


def test_reconcile_never_flips_an_offline_device_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale cache entry for a dead device must not claim ONLINE (the #1776 latch)."""
    device = _device(state=DeviceState.OFFLINE)
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    _seed_cache_hit(monitor, monkeypatch, {"version": "2026.6.4"})

    monitor.reconcile_from_mdns_cache("kitchen")

    assert device.state == DeviceState.OFFLINE
    assert callbacks.calls_for("on_state_change") == []


def test_reconcile_cache_miss_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No cached service → no callbacks and no wire traffic."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor._mdns._zeroconf = MagicMock()
    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    monitor.reconcile_from_mdns_cache("kitchen")

    assert callbacks.calls == []
    fake_info.async_request.assert_not_called()


def test_reconcile_without_zeroconf_is_a_noop() -> None:
    """Zeroconf failed to start → nothing to read; don't raise."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor._mdns._zeroconf = None

    monitor.reconcile_from_mdns_cache("kitchen")

    assert callbacks.calls == []


async def test_sweep_heals_blank_device_from_cache_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #1910 shape: blank record + populated cache → one sweep fills it, no API connect."""
    device = _device(ip="192.168.1.50", ip_addresses=["192.168.1.50"])
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _seed_cache_hit(
        monitor,
        monkeypatch,
        {"version": "2026.6.4", "config_hash": "abcd1234", "mac": "94c9601f8cf1"},
    )
    fetch = MagicMock()
    monitor._api_info._fetch = fetch  # type: ignore[method-assign]

    await monitor._api_info._sweep()

    assert device.deployed_version == "2026.6.4"
    assert device.deployed_config_hash == "abcd1234"
    assert device.mac_address == "94:C9:60:1F:8C:F1"
    assert device.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING
    fetch.assert_not_called()


def test_resolve_timeout_matches_upstream_default() -> None:
    """Service resolves get upstream esphome's window; 2s dropped slow ESP responders."""
    assert mdns_module._MDNS_RESOLVE_TIMEOUT_MS == DEFAULT_TIMEOUT_MS
