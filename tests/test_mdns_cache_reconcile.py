"""Tests for the level-triggered mDNS-cache reconcile path (issue #1910)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from esphome import zeroconf as esphome_zeroconf
from zeroconf import DNSText, current_time_millis
from zeroconf.const import _CLASS_IN, _TYPE_TXT

from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module
from esphome_device_builder.models import DeviceState, ReachabilitySource

from .conftest import make_online_api_device, make_state_monitor_with_callbacks

_SERVICE_NAME = "kitchen._esphomelib._tcp.local."
_HTTP_SERVICE_NAME = "kitchen._http._tcp.local."


def _txt_record(
    props: dict[str, str],
    *,
    age_ms: int = 1_000,
    ttl: int = 4500,
    service_name: str = _SERVICE_NAME,
) -> DNSText:
    payload = b"".join(
        bytes([len(entry)]) + entry for entry in (f"{k}={v}".encode() for k, v in props.items())
    )
    return DNSText(
        name=service_name,
        type_=_TYPE_TXT,
        class_=_CLASS_IN,
        ttl=ttl,
        text=payload,
        created=current_time_millis() - age_ms,
    )


def _seed_txt_cache(monitor: Any, records: list[DNSText]) -> None:
    """Wire a fake zeroconf whose cache serves each record for its own service name."""
    fake_zeroconf = MagicMock()
    fake_zeroconf.zeroconf.cache.get_all_by_details = MagicMock(
        side_effect=lambda name, *_a: [r for r in records if r.name == name]
    )
    monitor._mdns._zeroconf = fake_zeroconf


def test_reconcile_applies_txt_fields_without_claiming() -> None:
    """A cached TXT fills version/config_hash/mac/encryption but never state, IP, or ownership."""
    device = make_online_api_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _seed_txt_cache(
        monitor,
        [
            _txt_record(
                {
                    "version": "2026.6.4",
                    "config_hash": "abcd1234",
                    "mac": "94c9601f8cf1",
                    "api_encryption": "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
                }
            )
        ],
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


def test_reconcile_works_without_a_cached_address_record() -> None:
    """The steady state this exists for: TXT still cached (4500s TTL) after the A (120s) expired.

    ``AsyncServiceInfo.load_from_cache`` returns False without an
    unexpired address record, which is why the reconcile reads the
    TXT record directly.
    """
    device = make_online_api_device(ip="", ip_addresses=[])
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    _seed_txt_cache(monitor, [_txt_record({"version": "2026.6.4"}, age_ms=600_000)])

    monitor.reconcile_from_mdns_cache("kitchen")

    assert device.runtime_state.deployed_version == "2026.6.4"


def test_reconcile_never_flips_an_offline_device_online() -> None:
    """A stale cache entry for a dead device must not claim ONLINE (the #1776 latch)."""
    device = make_online_api_device(state=DeviceState.OFFLINE)
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    _seed_txt_cache(monitor, [_txt_record({"version": "2026.6.4"})])

    monitor.reconcile_from_mdns_cache("kitchen")

    assert device.runtime_state.state == DeviceState.OFFLINE
    assert callbacks.calls_for("on_state_change") == []


def test_reconcile_skips_expired_txt_records() -> None:
    """A TXT past its TTL is history, not a live payload; don't apply it."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    _seed_txt_cache(monitor, [_txt_record({"version": "2026.6.4"}, age_ms=5_000_000, ttl=4500)])

    monitor.reconcile_from_mdns_cache("kitchen")

    assert callbacks.calls == []


def test_reconcile_cache_miss_is_a_noop() -> None:
    """No cached TXT → no callbacks."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    _seed_txt_cache(monitor, [])

    monitor.reconcile_from_mdns_cache("kitchen")

    assert callbacks.calls == []


def test_reconcile_reads_http_identity_txt_for_non_api_device() -> None:
    """A cached ``_http._tcp`` TXT fills the identity trio; api_encryption is never driven."""
    device = make_online_api_device(api_enabled=False, loaded_integrations=["mqtt", "wifi"])
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _seed_txt_cache(
        monitor,
        [
            _txt_record(
                {
                    "version": "2026.8.0",
                    "config_hash": "abcd1234",
                    "mac": "94c9601f8cf1",
                    "api_encryption": "bogus",
                },
                service_name=_HTTP_SERVICE_NAME,
            )
        ],
    )

    monitor.reconcile_from_mdns_cache("kitchen")

    assert ("on_version_change", "kitchen", "2026.8.0") in callbacks.calls
    assert ("on_config_hash_change", "kitchen", "abcd1234") in callbacks.calls
    assert ("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1") in callbacks.calls
    assert callbacks.calls_for("on_api_encryption_change") == []
    assert callbacks.calls_for("on_state_change") == []
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING


def test_reconcile_http_txt_old_firmware_version_only() -> None:
    """An old-firmware fallback TXT (version only) fills version and nothing else."""
    device = make_online_api_device(api_enabled=False, loaded_integrations=["mqtt", "wifi"])
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    _seed_txt_cache(
        monitor, [_txt_record({"version": "2026.6.4"}, service_name=_HTTP_SERVICE_NAME)]
    )

    monitor.reconcile_from_mdns_cache("kitchen")

    assert device.runtime_state.deployed_version == "2026.6.4"
    assert callbacks.calls_for("on_config_hash_change") == []
    assert callbacks.calls_for("on_mac_address_change") == []


def test_reconcile_without_zeroconf_is_a_noop() -> None:
    """Zeroconf failed to start → nothing to read; don't raise."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    monitor._mdns._zeroconf = None

    monitor.reconcile_from_mdns_cache("kitchen")

    assert callbacks.calls == []


async def test_sweep_heals_blank_device_from_cache_end_to_end() -> None:
    """The #1910 shape: blank record + populated cache → one sweep fills it, no API connect."""
    device = make_online_api_device()
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _seed_txt_cache(
        monitor,
        [_txt_record({"version": "2026.6.4", "config_hash": "abcd1234", "mac": "94c9601f8cf1"})],
    )
    fetch = MagicMock()
    monitor._api_info._fetch = fetch  # type: ignore[method-assign]

    await monitor._api_info._sweep()

    assert device.runtime_state.deployed_version == "2026.6.4"
    assert device.runtime_state.deployed_config_hash == "abcd1234"
    assert device.mac_address == "94:C9:60:1F:8C:F1"
    assert device.runtime_state.state == DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == ReachabilitySource.PING
    fetch.assert_not_called()


async def test_sweep_heals_blank_non_api_device_from_http_cache() -> None:
    """A blank non-API device is repaired from its cached ``_http._tcp`` identity TXT."""
    device = make_online_api_device(api_enabled=False, loaded_integrations=["mqtt", "wifi"])
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["kitchen"] = ReachabilitySource.PING
    _seed_txt_cache(
        monitor,
        [
            _txt_record(
                {"version": "2026.8.0", "config_hash": "abcd1234", "mac": "94c9601f8cf1"},
                service_name=_HTTP_SERVICE_NAME,
            )
        ],
    )
    fetch = MagicMock()
    monitor._api_info._fetch = fetch  # type: ignore[method-assign]

    await monitor._api_info._sweep()

    assert device.runtime_state.deployed_version == "2026.8.0"
    assert device.runtime_state.deployed_config_hash == "abcd1234"
    assert device.mac_address == "94:C9:60:1F:8C:F1"
    fetch.assert_not_called()


async def test_resolve_then_dedupes_inflight_service_names() -> None:
    """A second resolve for a service already being resolved is dropped, not stacked."""
    monitor, _callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    source = monitor._mdns
    source._inflight_resolves.add(_SERVICE_NAME)
    info = MagicMock()
    info.name = _SERVICE_NAME
    info.async_request = AsyncMock()
    apply = MagicMock()

    await source._resolve_then(MagicMock(), info, "kitchen", apply)

    info.async_request.assert_not_called()
    apply.assert_not_called()
    # Still in flight — the guard entry belongs to the first resolver.
    assert _SERVICE_NAME in source._inflight_resolves


async def test_resolve_then_clears_inflight_after_completion() -> None:
    """The in-flight guard releases once the resolve finishes, so retries stay possible."""
    monitor, _callbacks = make_state_monitor_with_callbacks([make_online_api_device()])
    source = monitor._mdns
    info = MagicMock()
    info.name = _SERVICE_NAME
    info.async_request = AsyncMock(return_value=True)
    apply = MagicMock()

    await source._resolve_then(MagicMock(), info, "kitchen", apply)

    apply.assert_called_once_with("kitchen", info)
    assert _SERVICE_NAME not in source._inflight_resolves


def test_resolve_timeout_matches_upstream_default() -> None:
    """Local mirror of upstream esphome's resolve window; 2s dropped slow ESP responders.

    Mirrored (not imported) because ``DEFAULT_TIMEOUT_MS`` only exists
    in esphome >= 2025.5 while the declared floor is older; this pin
    catches upstream drift at CI time instead.
    """
    upstream = getattr(esphome_zeroconf, "DEFAULT_TIMEOUT_MS", None)
    if upstream is None:
        pytest.skip("installed esphome predates DEFAULT_TIMEOUT_MS")
    assert upstream == mdns_module._MDNS_RESOLVE_TIMEOUT_MS
