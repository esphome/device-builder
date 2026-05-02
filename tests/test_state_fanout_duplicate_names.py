"""
Tests for fan-out of state monitor callbacks across duplicate-named devices.

Two YAML files can declare the same ``name:`` value
(``foo.yaml`` and ``foo (1).yaml`` is the canonical case). They
share a single mDNS service announcement, so a state / IP /
version / config-hash / api-encryption observation has to fan
out to *every* configured device with that name — not just the
first one. The legacy behaviour returned the first match from
``next()``, which left the duplicate stuck at ``UNKNOWN`` while
its sibling tracked the device.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.models import Device, DeviceState


def _device(configuration: str, **overrides: Any) -> Device:
    base: dict[str, Any] = {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "configuration": configuration,
        "address": "kitchen.local",
        "state": DeviceState.UNKNOWN,
    }
    base.update(overrides)
    return Device(**base)


def _make_controller(devices: list[Device]) -> DevicesController:
    controller = DevicesController.__new__(DevicesController)
    controller._db = MagicMock()
    controller._scanner = MagicMock()
    controller._scanner.devices = devices
    return controller


def test_state_change_fans_out_to_every_matching_device() -> None:
    """``_on_state_change`` updates *every* device sharing the name."""
    a = _device("kitchen.yaml")
    b = _device("kitchen (1).yaml")
    controller = _make_controller([a, b])

    controller._on_state_change("kitchen", DeviceState.ONLINE, "mdns")

    assert a.state == DeviceState.ONLINE
    assert b.state == DeviceState.ONLINE


def test_ip_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", ip="")
    b = _device("kitchen (1).yaml", ip="")
    controller = _make_controller([a, b])
    # Drop the persist-async side effect; we only care about the
    # in-memory mutation here.
    controller._db.create_background_task = MagicMock()

    controller._on_ip_change("kitchen", "10.0.0.5")

    assert a.ip == "10.0.0.5"
    assert b.ip == "10.0.0.5"


def test_version_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", current_version="2026.5.0", deployed_version="")
    b = _device("kitchen (1).yaml", current_version="2026.5.0", deployed_version="")
    controller = _make_controller([a, b])
    controller._db.create_background_task = MagicMock()

    controller._on_version_change("kitchen", "2026.5.0")

    assert a.deployed_version == "2026.5.0"
    assert b.deployed_version == "2026.5.0"


def test_config_hash_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", expected_config_hash="abcd1234", deployed_config_hash="")
    b = _device(
        "kitchen (1).yaml",
        expected_config_hash="abcd1234",
        deployed_config_hash="",
    )
    controller = _make_controller([a, b])

    controller._on_config_hash_change("kitchen", "abcd1234")

    assert a.deployed_config_hash == "abcd1234"
    assert b.deployed_config_hash == "abcd1234"
    # Both devices' has_pending_changes should reflect the match.
    assert a.has_pending_changes is False
    assert b.has_pending_changes is False


def test_api_encryption_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", api_encryption_active=None)
    b = _device("kitchen (1).yaml", api_encryption_active=None)
    controller = _make_controller([a, b])

    controller._on_api_encryption_change("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")

    assert a.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    assert b.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"


def test_unrelated_devices_are_not_touched() -> None:
    """Devices with a different ``name`` field stay UNKNOWN."""
    kitchen = _device("kitchen.yaml")
    garage = _device("garage.yaml", name="garage", address="garage.local")
    controller = _make_controller([kitchen, garage])

    controller._on_state_change("kitchen", DeviceState.ONLINE, "mdns")

    assert kitchen.state == DeviceState.ONLINE
    assert garage.state == DeviceState.UNKNOWN
