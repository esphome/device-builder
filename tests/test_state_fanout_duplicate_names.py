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

from esphome_device_builder.models import Device, DeviceState

from .conftest import make_devices_controller_with_bus, make_state_monitor_with_callbacks


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


def test_state_change_fans_out_to_every_matching_device() -> None:
    """Fans the state update + bus event out to every matching device.

    ``_on_state_change`` has to update *every* device sharing the
    name and fire ``DEVICE_STATE_CHANGED`` once per configuration
    so each dashboard card redraws independently.
    """
    a = _device("kitchen.yaml")
    b = _device("kitchen (1).yaml")
    controller, captured = make_devices_controller_with_bus(
        [a, b],
        create_background_task=_close_coro,
    )

    controller._on_state_change("kitchen", DeviceState.ONLINE, "mdns")

    assert a.state == DeviceState.ONLINE
    assert b.state == DeviceState.ONLINE
    assert len(captured) == 2
    targeted = sorted(e.data["configuration"] for e in captured)
    assert targeted == ["kitchen (1).yaml", "kitchen.yaml"]


def test_ip_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", ip="")
    b = _device("kitchen (1).yaml", ip="")
    controller, captured = make_devices_controller_with_bus(
        [a, b],
        create_background_task=_close_coro,
    )

    controller._on_ip_change("kitchen", "10.0.0.5", ["10.0.0.5"])

    assert a.ip == "10.0.0.5"
    assert b.ip == "10.0.0.5"
    assert a.ip_addresses == ["10.0.0.5"]
    assert b.ip_addresses == ["10.0.0.5"]
    assert len(captured) == 2
    assert sorted(e.data["device"].configuration for e in captured) == [
        "kitchen (1).yaml",
        "kitchen.yaml",
    ]


def test_version_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", current_version="2026.5.0", deployed_version="")
    b = _device("kitchen (1).yaml", current_version="2026.5.0", deployed_version="")
    controller, captured = make_devices_controller_with_bus(
        [a, b],
        create_background_task=_close_coro,
    )

    controller._on_version_change("kitchen", "2026.5.0")

    assert a.deployed_version == "2026.5.0"
    assert b.deployed_version == "2026.5.0"
    assert len(captured) == 2
    assert sorted(e.data["device"].configuration for e in captured) == [
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
    controller, captured = make_devices_controller_with_bus(
        [a, b],
        create_background_task=_close_coro,
    )

    controller._on_config_hash_change("kitchen", "abcd1234")

    assert a.deployed_config_hash == "abcd1234"
    assert b.deployed_config_hash == "abcd1234"
    # Both devices' has_pending_changes should reflect the match.
    assert a.has_pending_changes is False
    assert b.has_pending_changes is False
    assert len(captured) == 2


def test_api_encryption_change_fans_out_to_every_matching_device() -> None:
    a = _device("kitchen.yaml", api_encryption_active=None)
    b = _device("kitchen (1).yaml", api_encryption_active=None)
    controller, captured = make_devices_controller_with_bus(
        [a, b],
        create_background_task=_close_coro,
    )

    controller._on_api_encryption_change("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")

    assert a.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    assert b.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    assert len(captured) == 2


def test_unrelated_devices_are_not_touched() -> None:
    """Devices with a different ``name`` field stay UNKNOWN."""
    kitchen = _device("kitchen.yaml")
    garage = _device("garage.yaml", name="garage", address="garage.local")
    controller, captured = make_devices_controller_with_bus(
        [kitchen, garage],
        create_background_task=_close_coro,
    )

    controller._on_state_change("kitchen", DeviceState.ONLINE, "mdns")

    assert kitchen.state == DeviceState.ONLINE
    assert garage.state == DeviceState.UNKNOWN
    assert len(captured) == 1
    assert captured[0].data["configuration"] == "kitchen.yaml"


def test_apply_state_repairs_stale_sibling_when_first_match_is_in_sync() -> None:
    """``apply()`` must fan out even when ``bucket[0]``'s state already matches.

    With duplicate ``esphome.name`` entries, ``_find_device_by_name``
    returns whichever device the scanner happens to have first in
    its bucket. If that one is already ONLINE (e.g. mDNS already
    flipped it) and a sibling was rebuilt with state=UNKNOWN
    (atomic-save churn etc.), the old "first device matches → bail"
    path skipped the fan-out and left the sibling stuck. Verify
    that ``apply()`` looks at *every* matching device's state and
    fires the callback when any one of them is stale.
    """
    primary = _device("kitchen.yaml")
    primary.state = DeviceState.ONLINE  # already in-sync
    sibling = _device("kitchen (1).yaml")  # state=UNKNOWN — was rebuilt
    monitor, callbacks = make_state_monitor_with_callbacks([primary, sibling])

    assert monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True) is True
    assert primary.state == DeviceState.ONLINE
    assert sibling.state == DeviceState.ONLINE
    assert callbacks.calls == [("on_state_change", "kitchen", DeviceState.ONLINE, "mdns")]
