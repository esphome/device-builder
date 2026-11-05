"""Tests for the descriptive ``network`` mDNS TXT key on configured devices."""

from __future__ import annotations

from unittest.mock import MagicMock

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import EventType

from .conftest import (
    make_device,
    make_devices_controller_with_bus,
    make_state_monitor_with_callbacks,
)


def test_first_observation_fires_once() -> None:
    """A new ``network`` value is forwarded once and deduped on repeat announces."""
    devices = [make_device()]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    monitor.mdns._apply_network_txt("kitchen", {"network": "wifi"})
    monitor.mdns._apply_network_txt("kitchen", {"network": "wifi"})

    assert devices[0].runtime_state.network == "wifi"
    assert callbacks.calls == [("on_network_change", "kitchen", "wifi")]


def test_absent_or_empty_key_never_blanks_known_value() -> None:
    """A missing or empty ``network`` leaves the known value alone."""
    devices = [make_device(network="wifi")]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    monitor.mdns._apply_network_txt("kitchen", {"version": "2026.8.2"})
    monitor.mdns._apply_network_txt("kitchen", {"network": ""})

    assert devices[0].runtime_state.network == "wifi"
    assert callbacks.calls == []


async def test_on_network_change_updates_device_persists_and_fires_event() -> None:
    """The controller callback updates the device, persists, and fires DEVICE_UPDATED."""
    device = make_device(network="")
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_network_change("kitchen", "ethernet")

    assert device.runtime_state.network == "ethernet"
    assert controller._metadata_store.get("kitchen.yaml")["network"] == "ethernet"
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)


def test_esphomelib_txt_applies_network_alongside_identity() -> None:
    """One ``_esphomelib._tcp`` announce populates identity and ``network`` together."""
    devices = [make_device()]
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)

    monitor.mdns._apply_txt_properties(
        "kitchen", {"version": "2026.8.2", "config_hash": "8600af66", "network": "wifi"}
    )

    assert devices[0].runtime_state.deployed_version == "2026.8.2"
    assert devices[0].runtime_state.network == "wifi"


def test_network_only_http_txt_does_not_vouch_for_identity() -> None:
    """A non-API ``_http._tcp`` TXT carrying only ``network`` applies it without vouching."""
    devices = [make_device(api_enabled=False)]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    monitor.mdns._apply_http_identity_props("kitchen", {"network": "wifi"})

    assert devices[0].runtime_state.network == "wifi"
    assert devices[0].runtime_state.deployed_identity_live is False
    assert callbacks.calls_for("on_deployed_identity_live_change") == []


def test_unwired_callback_drops_the_observation() -> None:
    """A monitor without ``on_network_change`` forwards nothing."""
    monitor = DeviceStateMonitor(
        get_devices=lambda: [make_device()], on_state_change=MagicMock(), on_ip_change=MagicMock()
    )

    assert monitor._apply_network("kitchen", "wifi") is False
