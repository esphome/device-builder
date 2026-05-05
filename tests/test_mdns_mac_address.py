"""Tests for mDNS-driven MAC-address sync.

ESPHome firmware broadcasts a ``mac`` TXT record on the
``_esphomelib._tcp`` mDNS service so dashboards can show the
hardware address without a separate query. The TXT value is the
lowercase 12-hex-char form (no colons); the frontend pretty-prints
to ``94:c9:60:1f:8c:f1`` at display time. Same monitor → controller
shape as the other TXT pipelines in ``test_mdns_version.py`` and
``test_mdns_config_hash.py``.

The MAC is persisted to the per-device metadata sidecar so it
renders immediately on backend restart — ESPHome devices stay
mDNS-silent until probed, which would otherwise leave the column
blank for several seconds. Persistence is gated on a real change
to keep the steady-state "same MAC every announce" cycle off-disk.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from esphome_device_builder.models import Device, DeviceState, EventType

from .conftest import make_devices_controller_with_bus, make_state_monitor_with_callbacks


def _device(**overrides: Any) -> Device:
    base: dict[str, Any] = {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "configuration": "kitchen.yaml",
        "address": "kitchen.local",
        "state": DeviceState.UNKNOWN,
    }
    base.update(overrides)
    return Device(**base)


def test_apply_mac_address_first_observation_fires_callback() -> None:
    """A MAC we haven't seen before reaches the controller."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_mac_address("kitchen", "94c9601f8cf1") is True
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94c9601f8cf1")]


def test_apply_mac_address_dedupes_same_value() -> None:
    """Same MAC twice → callback fires once.

    Devices broadcast the same TXT every announce; the dedupe keeps
    DEVICE_UPDATED quiet on a healthy fleet.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94c9601f8cf1")]


def test_apply_mac_address_fires_on_change() -> None:
    """A different MAC than the last observation re-fires the callback.

    Realistic when an unflashed YAML gets pointed at a different
    physical board mid-test.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    monitor.apply_mac_address("kitchen", "aabbccddeeff")
    assert callbacks.calls == [
        ("on_mac_address_change", "kitchen", "94c9601f8cf1"),
        ("on_mac_address_change", "kitchen", "aabbccddeeff"),
    ]


def test_apply_mac_address_ignores_empty_string() -> None:
    """Older firmware doesn't broadcast the TXT → empty-string is a no-op.

    The TXT extraction site (``_apply_service_info_to_device``)
    skips ``apply_mac_address`` entirely when the TXT is missing,
    but the apply method still has to drop empty strings on its own
    so callers that read the dict via ``.get("mac") or ""`` don't
    blank out a previously-known MAC.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_mac_address("kitchen", "") is False
    assert callbacks.calls == []


def test_apply_mac_address_unknown_device_is_no_op() -> None:
    """A stray announcement for an unconfigured name does nothing."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_mac_address("not-configured", "94c9601f8cf1") is False
    assert callbacks.calls == []


# ----------------------------------------------------------------------
# Normalization at ingest
#
# ESPHome firmware broadcasts a lowercase 12-hex-char MAC today, but
# the dashboard normalizes at ingest so the dedupe + persisted sidecar
# stay canonical even if a future firmware switches case or
# separator style. The wire is not the source of truth — the
# normalized form is.
# ----------------------------------------------------------------------


def test_apply_mac_address_normalizes_uppercase() -> None:
    """Uppercase MACs collapse to lowercase before storage."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_mac_address("kitchen", "94C9601F8CF1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94c9601f8cf1")]


def test_apply_mac_address_strips_colon_separators() -> None:
    """Colon-separated MACs land as 12 contiguous hex chars."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_mac_address("kitchen", "94:c9:60:1f:8c:f1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94c9601f8cf1")]


def test_apply_mac_address_strips_dash_separators() -> None:
    """Windows-style ``94-c9-60-...`` normalizes the same way."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_mac_address("kitchen", "94-C9-60-1F-8C-F1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94c9601f8cf1")]


def test_apply_mac_address_strips_dot_separators() -> None:
    """Cisco-style ``94c9.601f.8cf1`` normalizes the same way."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_mac_address("kitchen", "94c9.601f.8cf1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94c9601f8cf1")]


def test_apply_mac_address_normalized_dedupes_against_stored() -> None:
    """An uppercase re-broadcast of a stored lowercase MAC dedupes.

    The whole point of normalizing at ingest: the dashboard
    shouldn't write a sidecar entry every time the firmware happens
    to switch case style. The dedupe is keyed off the normalized
    form so equivalence holds across surface formats.
    """
    devices = [_device(mac_address="94c9601f8cf1")]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)
    assert monitor.apply_mac_address("kitchen", "94:C9:60:1F:8C:F1") is False
    assert callbacks.calls == []


def test_apply_mac_address_rejects_non_hex_input() -> None:
    """Garbage TXT content is dropped, not stored."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_mac_address("kitchen", "not-a-mac") is False
    assert callbacks.calls == []


def test_apply_mac_address_rejects_wrong_length() -> None:
    """Too-short / too-long values are dropped (not silently truncated)."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    # 11 chars
    assert monitor.apply_mac_address("kitchen", "94c9601f8cf") is False
    # 13 chars
    assert monitor.apply_mac_address("kitchen", "94c9601f8cf1a") is False
    assert callbacks.calls == []


def test_apply_mac_address_refires_after_device_rebuild() -> None:
    """A rebuilt Device with empty MAC gets repopulated by the next mDNS event.

    Atomic-write editor races (vscode-on-macOS et al.) can briefly
    REMOVE+re-ADD a device with ``previous=None``, leaving the new
    Device with ``mac_address=""``. The monitor's dedupe is keyed off
    the device's own field, so the next mDNS announcement should
    repopulate without short-circuiting on a stale cache.
    """
    devices = [_device(mac_address="94c9601f8cf1")]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    # Steady state: the device already has the MAC, so a repeat
    # announcement is a no-op.
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    assert callbacks.calls == []

    # Atomic-save churn rebuilds the Device with empty fields. The
    # next mDNS announcement should write the MAC back through the
    # callback.
    devices[0].mac_address = ""
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94c9601f8cf1")]


# ----------------------------------------------------------------------
# DevicesController._on_mac_address_change — full pipe + persistence
# ----------------------------------------------------------------------


def _record_scheduled(coros: list[object]) -> Callable[[object], object]:
    """Capture + close coroutines handed to ``create_background_task``.

    The persist-async branches use ``create_background_task`` to push
    the blocking sidecar write off the event-loop thread. The tests
    don't have a running loop, so we just record the coroutine and
    close it to avoid the "coroutine was never awaited" warning —
    the call count is what verifies whether the I/O was scheduled.
    """

    def _impl(coro: object) -> object:
        coros.append(coro)
        if hasattr(coro, "close"):
            coro.close()
        return coro

    return _impl


def _device_kitchen(**overrides: Any) -> Device:
    base: dict[str, Any] = {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "configuration": "kitchen.yaml",
    }
    base.update(overrides)
    return Device(**base)


def test_on_mac_address_change_updates_device_and_fires_event() -> None:
    """Full pipe: callback writes the MAC + fires DEVICE_UPDATED."""
    device = _device_kitchen()
    scheduled: list[object] = []
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94c9601f8cf1")

    assert device.mac_address == "94c9601f8cf1"
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)


def test_on_mac_address_change_persists_to_sidecar() -> None:
    """First observation schedules exactly one sidecar write."""
    device = _device_kitchen()
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94c9601f8cf1")

    assert len(scheduled) == 1


def test_on_mac_address_change_skips_persist_when_unchanged() -> None:
    """Repeat observation of the same MAC must not schedule any I/O.

    mDNS announces the same TXT every cycle on a healthy fleet; a
    naive write-through would hammer the sidecar on every announce.
    The dedupe is keyed off ``device.mac_address`` so a steady-state
    broadcast short-circuits before either the in-memory write or
    the executor-bound ``set_device_metadata`` call.
    """
    device = _device_kitchen(mac_address="94c9601f8cf1")
    scheduled: list[object] = []
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94c9601f8cf1")

    assert scheduled == []
    assert captured == []


def test_on_mac_address_change_unknown_device_is_noop() -> None:
    """Stray callback for an unconfigured name doesn't raise or fire events."""
    scheduled: list[object] = []
    controller, captured = make_devices_controller_with_bus(
        [], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_mac_address_change("ghost", "94c9601f8cf1")

    assert scheduled == []
    assert captured == []


def test_on_mac_address_change_derives_ethernet_mac_on_esp32() -> None:
    """ESP32 + ethernet integration → ``ethernet_mac`` = primary + 3.

    The primary stays at the broadcast value; the derived MAC is
    written to ``device.ethernet_mac`` so the drawer can render the
    second row without the firmware having to broadcast it.
    """
    device = _device_kitchen(
        target_platform="esp32",
        loaded_integrations=["api", "wifi", "ethernet"],
    )
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94c9601f8cf0")

    assert device.mac_address == "94c9601f8cf0"
    assert device.ethernet_mac == "94c9601f8cf3"
    assert device.bluetooth_mac == ""


def test_on_mac_address_change_derives_bluetooth_mac_on_esp32() -> None:
    """ESP32 + bluetooth integration → ``bluetooth_mac`` = primary + 2."""
    device = _device_kitchen(
        target_platform="esp32",
        loaded_integrations=["api", "wifi", "esp32_ble_tracker"],
    )
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94c9601f8cf0")

    assert device.mac_address == "94c9601f8cf0"
    assert device.ethernet_mac == ""
    assert device.bluetooth_mac == "94c9601f8cf2"


def test_on_mac_address_change_derives_ethernet_equal_primary_on_rp2040() -> None:
    """RP2040 + ethernet → derived ethernet equals the primary MAC.

    Single-MAC platform: the dashboard exposes the field for shape
    consistency with ESP32 but the frontend hides any row whose
    derived value equals the primary so the row doesn't render twice.
    """
    device = _device_kitchen(
        target_platform="rp2040",
        loaded_integrations=["api", "wifi", "ethernet"],
    )
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94c9601f8cf0")

    assert device.mac_address == "94c9601f8cf0"
    assert device.ethernet_mac == "94c9601f8cf0"
    assert device.bluetooth_mac == ""


def test_on_mac_address_change_clears_derived_on_unknown_platform() -> None:
    """A platform we haven't validated against the eFuse layout → empty derivations.

    Belt-and-braces: even if a future ESPHome adds a platform key
    ahead of the dashboard's allowlist update, we'd rather show a
    single primary MAC than a wrong derived one.
    """
    device = _device_kitchen(
        target_platform="bk72xx",
        loaded_integrations=["api", "ethernet"],
    )
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94c9601f8cf0")

    assert device.mac_address == "94c9601f8cf0"
    assert device.ethernet_mac == ""
    assert device.bluetooth_mac == ""
