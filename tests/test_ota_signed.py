"""Tests for the ``ota_signed`` mDNS flag and the YAML ``signing_key`` signal."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.helpers.device_yaml import load_device_from_storage
from esphome_device_builder.models import EventType
from tests._storage_fixtures import write_storage_json

from .conftest import (
    make_device,
    make_devices_controller_with_bus,
    make_state_monitor_with_callbacks,
)

_SIGNED_YAML = (
    "esphome:\n  name: lamp\n"
    "esp32:\n  framework:\n    type: esp-idf\n    advanced:\n"
    "      signed_ota_verification:\n"
)


def _signed_calls(callbacks) -> list[tuple]:
    return callbacks.calls_for("on_ota_signed_change")


def test_txt_ota_signed_sets_flag() -> None:
    """``ota_signed=1`` on the esphomelib TXT marks the device."""
    device = make_device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])

    monitor.mdns._apply_txt_properties("kitchen", {"version": "2026.10.0", "ota_signed": "1"})

    assert _signed_calls(callbacks) == [("on_ota_signed_change", "kitchen", True)]
    assert device.runtime_state.ota_signed is True


def test_txt_without_key_clears_flag() -> None:
    """A content-bearing announce without the key clears a prior ``True``."""
    device = make_device()
    device.runtime_state.ota_signed = True
    monitor, callbacks = make_state_monitor_with_callbacks([device])

    monitor.mdns._apply_txt_properties("kitchen", {"version": "2026.10.0"})

    assert _signed_calls(callbacks) == [("on_ota_signed_change", "kitchen", False)]
    assert device.runtime_state.ota_signed is False


def test_empty_txt_keeps_flag() -> None:
    """An empty TXT (cache eviction / fragment) leaves the flag alone."""
    device = make_device()
    device.runtime_state.ota_signed = True
    monitor, callbacks = make_state_monitor_with_callbacks([device])

    monitor.mdns._apply_txt_properties("kitchen", {})

    assert _signed_calls(callbacks) == []
    assert device.runtime_state.ota_signed is True


def test_repeated_announce_dedupes() -> None:
    """An unchanged value does not re-fire the callback."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    props = {"version": "2026.10.0", "ota_signed": "1"}

    monitor.mdns._apply_txt_properties("kitchen", props)
    monitor.mdns._apply_txt_properties("kitchen", props)

    assert _signed_calls(callbacks) == [("on_ota_signed_change", "kitchen", True)]


def test_apply_without_callback_is_noop() -> None:
    """An unwired ``on_ota_signed_change`` drops the observation."""
    device = make_device()
    monitor = DeviceStateMonitor(
        get_devices=lambda: [device],
        on_state_change=lambda *_a: None,
        on_ip_change=lambda *_a: None,
    )

    assert monitor._apply_ota_signed("kitchen", signed=True) is False
    assert device.runtime_state.ota_signed is False


async def test_controller_callback_updates_device_and_fires_event() -> None:
    """The controller writes ``runtime_state.ota_signed`` and fires DEVICE_UPDATED."""
    device = make_device()
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_ota_signed_change("kitchen", signed=True)

    assert device.runtime_state.ota_signed is True
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)


async def test_controller_callback_skips_unchanged() -> None:
    """An unchanged value fires no event."""
    device = make_device()
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_ota_signed_change("kitchen", signed=False)

    assert captured == []


def test_adoptable_carries_ota_signed() -> None:
    """``_build_adoptable`` copies ``DiscoveredImport.ota_signed``."""
    monitor, _callbacks = make_state_monitor_with_callbacks([])
    discovered = SimpleNamespace(
        friendly_name="Kitchen",
        device_name="kitchen-1a2b3c",
        package_import_url="github://acme/firmware/kitchen.yaml@main",
        project_name="acme.kitchen",
        project_version="2026.10.0",
        network="wifi",
        ota_signed=True,
    )

    assert monitor.importable._build_adoptable(discovered).ota_signed is True


def test_adoptable_defaults_false_without_field() -> None:
    """A ``DiscoveredImport`` predating the field yields ``False``."""
    monitor, _callbacks = make_state_monitor_with_callbacks([])
    discovered = SimpleNamespace(
        friendly_name="Kitchen",
        device_name="kitchen-1a2b3c",
        package_import_url="github://acme/firmware/kitchen.yaml@main",
        project_name="acme.kitchen",
        project_version="2026.9.0",
        network="wifi",
    )

    assert monitor.importable._build_adoptable(discovered).ota_signed is False


def test_load_device_ota_signing_key(tmp_path: Path) -> None:
    """esp32 ``signed_ota_verification`` with ``signing_key`` arms the flag."""
    yaml_path = tmp_path / "lamp.yaml"
    yaml_path.write_text(_SIGNED_YAML + "        signing_key: key.pem\n", encoding="utf-8")
    write_storage_json(tmp_path, "lamp.yaml")

    assert load_device_from_storage(yaml_path).ota_signing_key is True


def test_load_device_ota_signing_key_bare_block(tmp_path: Path) -> None:
    """A bare ``signed_ota_verification:`` block (no key) leaves the flag off."""
    yaml_path = tmp_path / "lamp.yaml"
    yaml_path.write_text(_SIGNED_YAML, encoding="utf-8")
    write_storage_json(tmp_path, "lamp.yaml")

    assert load_device_from_storage(yaml_path).ota_signing_key is False


def test_load_device_ota_signing_key_non_esp32(tmp_path: Path) -> None:
    """The flag stays off on non-esp32 platforms."""
    yaml_path = tmp_path / "lamp.yaml"
    yaml_path.write_text(_SIGNED_YAML + "        signing_key: key.pem\n", encoding="utf-8")
    write_storage_json(tmp_path, "lamp.yaml", overrides={"core_platform": "esp8266"})

    assert load_device_from_storage(yaml_path).ota_signing_key is False
