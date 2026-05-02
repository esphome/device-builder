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


def _close_coro(coro: Any) -> Any:
    """Close any coroutine handed to ``create_background_task``.

    Without this the test stub leaves ``_persist_device_ip_async`` /
    ``_persist_storage_version_async`` coroutines un-awaited, which
    triggers ``RuntimeWarning: coroutine was never awaited`` (some
    pytest configs upgrade that to a failure).
    """
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


def _make_controller(devices: list[Device]) -> DevicesController:
    controller = DevicesController.__new__(DevicesController)
    controller._db = MagicMock()
    controller._db.create_background_task = MagicMock(side_effect=_close_coro)
    controller._db.bus = MagicMock()
    controller._scanner = MagicMock()
    controller._scanner.devices = devices
    return controller


def _fired_events(controller: DevicesController) -> list[tuple[Any, dict[str, Any]]]:
    """All ``(event_type, data)`` pairs forwarded to the event bus."""
    return [call.args for call in controller._db.bus.fire.call_args_list]


def test_state_change_fans_out_to_every_matching_device() -> None:
    """Fans the state update + bus event out to every matching device.

    ``_on_state_change`` has to update *every* device sharing the
    name and fire ``DEVICE_STATE_CHANGED`` once per configuration
    so each dashboard card redraws independently.
    """
    a = _device("kitchen.yaml")
    b = _device("kitchen (1).yaml")
    controller = _make_controller([a, b])

    controller._on_state_change("kitchen", DeviceState.ONLINE, "mdns")

    assert a.state == DeviceState.ONLINE
    assert b.state == DeviceState.ONLINE
    fired = _fired_events(controller)
    assert len(fired) == 2
    targeted = sorted(data["configuration"] for _et, data in fired)
    assert targeted == ["kitchen (1).yaml", "kitchen.yaml"]


def test_ip_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", ip="")
    b = _device("kitchen (1).yaml", ip="")
    controller = _make_controller([a, b])

    controller._on_ip_change("kitchen", "10.0.0.5")

    assert a.ip == "10.0.0.5"
    assert b.ip == "10.0.0.5"
    fired = _fired_events(controller)
    assert len(fired) == 2
    assert sorted(data["device"].configuration for _et, data in fired) == [
        "kitchen (1).yaml",
        "kitchen.yaml",
    ]


def test_version_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", current_version="2026.5.0", deployed_version="")
    b = _device("kitchen (1).yaml", current_version="2026.5.0", deployed_version="")
    controller = _make_controller([a, b])

    controller._on_version_change("kitchen", "2026.5.0")

    assert a.deployed_version == "2026.5.0"
    assert b.deployed_version == "2026.5.0"
    fired = _fired_events(controller)
    assert len(fired) == 2
    assert sorted(data["device"].configuration for _et, data in fired) == [
        "kitchen (1).yaml",
        "kitchen.yaml",
    ]


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
    fired = _fired_events(controller)
    assert len(fired) == 2


def test_api_encryption_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", api_encryption_active=None)
    b = _device("kitchen (1).yaml", api_encryption_active=None)
    controller = _make_controller([a, b])

    controller._on_api_encryption_change("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")

    assert a.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    assert b.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    fired = _fired_events(controller)
    assert len(fired) == 2


def test_unrelated_devices_are_not_touched() -> None:
    """Devices with a different ``name`` field stay UNKNOWN."""
    kitchen = _device("kitchen.yaml")
    garage = _device("garage.yaml", name="garage", address="garage.local")
    controller = _make_controller([kitchen, garage])

    controller._on_state_change("kitchen", DeviceState.ONLINE, "mdns")

    assert kitchen.state == DeviceState.ONLINE
    assert garage.state == DeviceState.UNKNOWN
    fired = _fired_events(controller)
    assert len(fired) == 1
    assert fired[0][1]["configuration"] == "kitchen.yaml"
