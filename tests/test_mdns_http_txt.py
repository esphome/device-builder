"""
Reading the identity TXT off a non-API device's ``_http._tcp`` mDNS service.

New firmware publishes ``version`` / ``mac`` / ``config_hash`` there;
older firmware carries ``version`` only. ``MdnsSource``'s HTTP handler
applies whichever keys are present and never api_encryption.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from zeroconf import ServiceStateChange

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.controllers._device_state_monitor._state import MonitorState
from esphome_device_builder.controllers._device_state_monitor.importable import ImportableDiscovery
from esphome_device_builder.controllers._device_state_monitor.mdns import MdnsSource
from esphome_device_builder.controllers._device_state_monitor.ping import PingSource
from esphome_device_builder.models import Device

_HTTP = "_http._tcp.local."


def _make_monitor(*devices: Device) -> DeviceStateMonitor:
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)
    monitor.state = MonitorState()
    monitor._importable = ImportableDiscovery(monitor)
    monitor._mdns = MdnsSource(monitor)
    monitor._presence = None
    monitor._ping = PingSource(monitor)
    monitor._mdns._zeroconf = MagicMock()
    monitor._mdns._zeroconf.zeroconf = MagicMock()
    monitor._tasks = set()
    monitor.state.reachability = None
    monitor._get_devices = lambda: list(devices)
    monitor._get_devices_by_name = lambda name: [d for d in devices if d.name == name]
    return monitor


def _mqtt_device(**overrides: Any) -> Device:
    base: dict[str, Any] = {
        "name": "klo",
        "friendly_name": "Klo",
        "configuration": "klo.yaml",
        "address": "klo.local",
        "api_enabled": False,
    }
    base.update(overrides)
    return Device(**base)


def _capture_apply(monitor: DeviceStateMonitor, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(
        monitor._mdns, "_apply_http_txt", lambda name, info: calls.append((name, info))
    )
    return calls


def _cached_info(monkeypatch: pytest.MonkeyPatch, *, cached: bool) -> MagicMock:
    info = MagicMock()
    info.load_from_cache.return_value = cached
    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.mdns.AsyncServiceInfo",
        lambda *_a, **_k: info,
    )
    return info


# ----------------------------------------------------------------------
# _on_http_service_state_change — routing
# ----------------------------------------------------------------------


def test_http_cache_hit_applies_identity_for_non_api_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached ``_http._tcp`` service for a configured non-API device applies inline."""
    monitor = _make_monitor(_mqtt_device())
    calls = _capture_apply(monitor, monkeypatch)
    info = _cached_info(monkeypatch, cached=True)

    monitor._mdns._on_http_service_state_change(
        MagicMock(), _HTTP, f"klo.{_HTTP}", ServiceStateChange.Added
    )

    assert calls == [("klo", info)]
    assert not monitor._tasks


async def test_http_cache_miss_spawns_resolve_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache miss → fire-and-forget resolve task tracked in ``_tasks``."""
    monitor = _make_monitor(_mqtt_device())
    calls = _capture_apply(monitor, monkeypatch)
    _cached_info(monkeypatch, cached=False)

    async def fake_resolve(*_args, **_kw) -> None:
        return None

    monkeypatch.setattr(monitor._mdns, "_resolve_then", fake_resolve)

    monitor._mdns._on_http_service_state_change(
        MagicMock(), _HTTP, f"klo.{_HTTP}", ServiceStateChange.Added
    )

    assert calls == []
    assert len(monitor._tasks) == 1
    await asyncio.gather(*monitor._tasks)


def test_http_skips_all_api_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """An all-API name bucket gets its identity from the esphomelib path; HTTP is a no-op."""
    monitor = _make_monitor(_mqtt_device(api_enabled=True))
    calls = _capture_apply(monitor, monkeypatch)
    _cached_info(monkeypatch, cached=True)

    monitor._mdns._on_http_service_state_change(
        MagicMock(), _HTTP, f"klo.{_HTTP}", ServiceStateChange.Added
    )

    assert calls == []


def test_http_applies_when_bucket_has_a_non_api_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name shared by an API and a non-API config still applies (whole-bucket check)."""
    monitor = _make_monitor(
        _mqtt_device(api_enabled=True),
        _mqtt_device(api_enabled=False, configuration="klo (1).yaml"),
    )
    calls = _capture_apply(monitor, monkeypatch)
    info = _cached_info(monkeypatch, cached=True)

    monitor._mdns._on_http_service_state_change(
        MagicMock(), _HTTP, f"klo.{_HTTP}", ServiceStateChange.Added
    )

    assert calls == [("klo", info)]


def test_http_skips_unconfigured_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``_http._tcp`` from a name we don't have configured is ignored."""
    monitor = _make_monitor()
    calls = _capture_apply(monitor, monkeypatch)
    _cached_info(monkeypatch, cached=True)

    monitor._mdns._on_http_service_state_change(
        MagicMock(), _HTTP, f"printer.{_HTTP}", ServiceStateChange.Added
    )

    assert calls == []


def test_http_removed_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """We never drive state off an HTTP ``Removed``; reachability owns that."""
    monitor = _make_monitor(_mqtt_device())
    calls = _capture_apply(monitor, monkeypatch)

    monitor._mdns._on_http_service_state_change(
        MagicMock(), _HTTP, f"klo.{_HTTP}", ServiceStateChange.Removed
    )

    assert calls == []


# ----------------------------------------------------------------------
# _apply_http_txt — TXT extraction
# ----------------------------------------------------------------------


def _capture_monitor_applies(
    monitor: DeviceStateMonitor, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str, str]]:
    applied: list[tuple[str, str, str]] = []
    for method in ("apply_version", "apply_config_hash", "apply_mac_address"):
        monkeypatch.setattr(
            monitor, method, lambda name, value, m=method: applied.append((m, name, value))
        )
    monkeypatch.setattr(
        monitor,
        "apply_api_encryption",
        lambda name, value: pytest.fail("api_encryption must never be driven from _http._tcp"),
    )
    return applied


def _http_info(props: dict[str, str]) -> MagicMock:
    info = MagicMock()
    info.decoded_properties = props
    return info


def test_apply_http_txt_reads_identity_trio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Version / config_hash / mac reach their apply methods; api_encryption never does."""
    monitor = _make_monitor(_mqtt_device())
    applied = _capture_monitor_applies(monitor, monkeypatch)

    # The stray api_encryption key is ignored, not applied (the capture
    # helper fails the test if it ever reaches the monitor).
    monitor._mdns._apply_http_txt(
        "klo",
        _http_info(
            {
                "version": "2026.8.0",
                "config_hash": "5a94a12d",
                "mac": "94c9601f8cf1",
                "api_encryption": "Noise_NNpsk0",
            }
        ),
    )

    assert applied == [
        ("apply_version", "klo", "2026.8.0"),
        ("apply_config_hash", "klo", "5a94a12d"),
        ("apply_mac_address", "klo", "94c9601f8cf1"),
    ]


def test_apply_http_txt_old_firmware_version_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old-firmware fallback carrying only ``version`` applies that and nothing else."""
    monitor = _make_monitor(_mqtt_device())
    applied = _capture_monitor_applies(monitor, monkeypatch)

    monitor._mdns._apply_http_txt("klo", _http_info({"version": "2026.6.4"}))

    assert applied == [("apply_version", "klo", "2026.6.4")]


def test_apply_http_txt_empty_props_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old web_server ``_http._tcp`` carries no TXT at all → nothing applied."""
    monitor = _make_monitor(_mqtt_device())
    applied = _capture_monitor_applies(monitor, monkeypatch)

    monitor._mdns._apply_http_txt("klo", _http_info({}))

    assert applied == []
