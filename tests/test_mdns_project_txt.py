"""
Tests for the descriptive mDNS TXT keys: project_name / project_version / network.

ESPHome broadcasts the ``esphome: project:`` pair and the link it
announced over alongside the identity trio (``version`` /
``config_hash`` / ``mac``). They ride the same monitor → controller →
sidecar pipeline, but deliberately sit outside
``_IDENTITY_TXT_APPLIERS``: they describe the firmware, never vouch
for its freshness, so a descriptive-only TXT must not stamp
``deployed_identity_live``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, EventType

from .conftest import (
    make_device,
    make_devices_controller_with_bus,
    make_state_monitor_with_callbacks,
)


def _device(**overrides: Any) -> Device:
    overrides.setdefault("current_version", "2026.5.0")
    return make_device(**overrides)


# ----------------------------------------------------------------------
# MdnsSource._apply_descriptive_txt — the TXT ingest seam
# ----------------------------------------------------------------------


def _apply(monitor: DeviceStateMonitor, name: str, **props: str) -> None:
    """Drive one announce's descriptive TXT keys through the mDNS source."""
    monitor.mdns._apply_descriptive_txt(name, dict(props))


def test_first_observation_fires_callbacks() -> None:
    """Keys we haven't seen before reach the controller."""
    devices = [_device()]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    _apply(
        monitor,
        "kitchen",
        project_name="dcoulson.ble-rrn00-wroom06",
        project_version="2026.09.06.0+ble2026.09.12.0",
        network="wifi",
    )

    runtime = devices[0].runtime_state
    assert runtime.project_name == "dcoulson.ble-rrn00-wroom06"
    assert runtime.project_version == "2026.09.06.0+ble2026.09.12.0"
    assert runtime.network == "wifi"
    assert callbacks.calls == [
        ("on_project_name_change", "kitchen", "dcoulson.ble-rrn00-wroom06"),
        ("on_project_version_change", "kitchen", "2026.09.06.0+ble2026.09.12.0"),
        ("on_network_change", "kitchen", "wifi"),
    ]


def test_dedupes_same_values() -> None:
    """Two identical announces fire once; mDNS announcements are noisy."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])

    _apply(monitor, "kitchen", project_name="apollo.plt-1", network="wifi")
    _apply(monitor, "kitchen", project_name="apollo.plt-1", network="wifi")

    assert callbacks.calls == [
        ("on_project_name_change", "kitchen", "apollo.plt-1"),
        ("on_network_change", "kitchen", "wifi"),
    ]


def test_fires_again_when_project_version_changes() -> None:
    """A re-flashed project version is forwarded; the unchanged name is not."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])

    _apply(monitor, "kitchen", project_name="apollo.plt-1", project_version="2026.09.06.0")
    _apply(monitor, "kitchen", project_name="apollo.plt-1", project_version="2026.09.12.0")

    assert callbacks.calls == [
        ("on_project_name_change", "kitchen", "apollo.plt-1"),
        ("on_project_version_change", "kitchen", "2026.09.06.0"),
        ("on_project_version_change", "kitchen", "2026.09.12.0"),
    ]


def test_absent_keys_never_blank_known_values() -> None:
    """Firmware with no project / pre-2023.6 network key leaves what we know alone."""
    devices = [_device(project_name="apollo.plt-1", network="wifi")]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    _apply(monitor, "kitchen", version="2026.8.2")

    assert devices[0].runtime_state.project_name == "apollo.plt-1"
    assert devices[0].runtime_state.network == "wifi"
    assert callbacks.calls == []


def test_empty_values_are_dropped() -> None:
    """An explicitly empty TXT value is treated as absent, not as a clear."""
    devices = [_device(network="wifi")]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    _apply(monitor, "kitchen", network="", project_name="")

    assert devices[0].runtime_state.network == "wifi"
    assert callbacks.calls == []


def test_network_last_announce_wins() -> None:
    """A dual-interface device follows the freshest announce, not the first."""
    devices = [_device()]
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)

    _apply(monitor, "kitchen", network="wifi")
    _apply(monitor, "kitchen", network="ethernet")

    assert devices[0].runtime_state.network == "ethernet"


def test_ignores_unknown_device() -> None:
    """Stray announcements for devices not in the catalog are dropped."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])

    _apply(monitor, "ghost", project_name="apollo.plt-1")

    assert callbacks.calls == []


def test_unwired_callbacks_silently_drop() -> None:
    """Without wired callbacks (older test setups) we don't raise."""
    monitor = DeviceStateMonitor(
        get_devices=lambda: [_device()],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_project_name_change=None,
        on_project_version_change=None,
        on_network_change=None,
    )

    _apply(monitor, "kitchen", project_name="apollo.plt-1", network="wifi")


def test_refires_after_device_rebuild() -> None:
    """A rebuilt Device with an empty project gets repopulated by the next announce.

    Same contract as the identity keys: the dedupe reads the device's
    own field, so an atomic-save REMOVE + re-ADD (``previous=None``)
    is observable and the next announcement refills it.
    """
    devices = [_device()]
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)

    _apply(monitor, "kitchen", project_name="apollo.plt-1")
    assert devices[0].runtime_state.project_name == "apollo.plt-1"

    devices[0] = _device()
    assert devices[0].runtime_state.project_name == ""

    _apply(monitor, "kitchen", project_name="apollo.plt-1")
    assert devices[0].runtime_state.project_name == "apollo.plt-1"


# ----------------------------------------------------------------------
# DevicesController callbacks
# ----------------------------------------------------------------------


async def test_on_project_name_change_updates_device_and_fires_event() -> None:
    """The full pipe: callback updates the in-memory device + fires DEVICE_UPDATED."""
    device = _device(project_name="")
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_project_name_change("kitchen", "apollo.plt-1")

    assert device.runtime_state.project_name == "apollo.plt-1"
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)


async def test_on_project_version_change_persists_to_metadata_store() -> None:
    """The value is persisted so the table can sort a cold-loaded fleet."""
    device = _device(project_version="")
    controller, _captured = make_devices_controller_with_bus([device])

    controller._on_project_version_change("kitchen", "2026.09.06.0")

    assert controller._metadata_store.get("kitchen.yaml")["project_version"] == "2026.09.06.0"


async def test_on_network_change_persists_to_metadata_store() -> None:
    """``network`` persists too — an offline row still reports its link."""
    device = _device(network="")
    controller, _captured = make_devices_controller_with_bus([device])

    controller._on_network_change("kitchen", "ethernet")

    assert device.runtime_state.network == "ethernet"
    assert controller._metadata_store.get("kitchen.yaml")["network"] == "ethernet"


async def test_on_project_name_change_skips_when_same() -> None:
    """No-op when the in-memory device already carries the announced project."""
    device = _device(project_name="apollo.plt-1")
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_project_name_change("kitchen", "apollo.plt-1")

    assert captured == []


async def test_on_network_change_unknown_device_is_noop() -> None:
    """A stray callback for an unknown device must not raise or fire events."""
    controller, captured = make_devices_controller_with_bus([])

    controller._on_network_change("ghost", "wifi")

    assert captured == []


# ----------------------------------------------------------------------
# MdnsSource — TXT ingest on both service paths
# ----------------------------------------------------------------------


def test_txt_properties_apply_descriptive_keys_alongside_identity() -> None:
    """One ``_esphomelib._tcp`` announce populates identity and descriptive keys together."""
    devices = [_device()]
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)

    monitor.mdns._apply_txt_properties(
        "kitchen",
        {
            "version": "2026.8.2",
            "config_hash": "8600af66",
            "project_name": "dcoulson.ble-rrn00-wroom06",
            "project_version": "2026.09.06.0+ble2026.09.12.0",
            "network": "wifi",
        },
    )

    runtime = devices[0].runtime_state
    assert runtime.deployed_version == "2026.8.2"
    assert runtime.project_name == "dcoulson.ble-rrn00-wroom06"
    assert runtime.project_version == "2026.09.06.0+ble2026.09.12.0"
    assert runtime.network == "wifi"


def test_http_txt_applies_descriptive_keys_for_non_api_devices() -> None:
    """A non-API device's ``_http._tcp`` TXT carries the same descriptive set."""
    devices = [_device(api_enabled=False)]
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)

    monitor.mdns._apply_http_identity_props(
        "kitchen",
        {"version": "2026.8.2", "project_name": "apollo.plt-1", "network": "ethernet"},
    )

    assert devices[0].runtime_state.project_name == "apollo.plt-1"
    assert devices[0].runtime_state.network == "ethernet"


def test_descriptive_only_http_txt_does_not_vouch_for_identity() -> None:
    """A TXT with no identity key must not stamp ``deployed_identity_live``.

    The reason the descriptive keys sit in their own applier tuple: a
    flag-True device stops re-resolving, so letting ``project_name``
    alone vouch would leave a stale identity permanently unverified.
    """
    devices = [_device(api_enabled=False)]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    monitor.mdns._apply_http_identity_props(
        "kitchen", {"project_name": "apollo.plt-1", "network": "wifi"}
    )

    assert devices[0].runtime_state.project_name == "apollo.plt-1"
    assert devices[0].runtime_state.deployed_identity_live is False
    assert callbacks.calls_for("on_deployed_identity_live_change") == []


def test_identity_key_still_vouches_when_descriptive_keys_ride_along() -> None:
    """The identity trio keeps its vouching contract unchanged."""
    devices = [_device(api_enabled=False)]
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)

    monitor.mdns._apply_http_identity_props(
        "kitchen", {"version": "2026.8.2", "project_name": "apollo.plt-1"}
    )

    assert devices[0].runtime_state.deployed_identity_live is True
