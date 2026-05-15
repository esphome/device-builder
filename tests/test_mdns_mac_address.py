"""Tests for mDNS-driven MAC-address sync.

ESPHome firmware broadcasts a ``mac`` TXT record on the
``_esphomelib._tcp`` mDNS service so dashboards can show the
hardware address without a separate query. The TXT value is the
lowercase 12-hex-char form (no colons); the dashboard normalizes
at ingest to the canonical ``XX:XX:XX:XX:XX:XX`` form so the
in-memory model, sidecar, and frontend wire all carry one shape
regardless of which case / separator style the firmware happens
to broadcast. Same monitor → controller pipeline as the other TXT
records covered by ``test_mdns_version.py`` /
``test_mdns_config_hash.py``.

The MAC is persisted to the per-device metadata sidecar so it
renders immediately on backend restart — ESPHome devices stay
mDNS-silent until probed, which would otherwise leave the column
blank for several seconds. Persistence is gated on a real change
to keep the steady-state "same MAC every announce" cycle off-disk.
"""

from __future__ import annotations

from typing import Any

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, EventType

from .conftest import (
    make_device,
    make_devices_controller_with_bus,
    make_state_monitor_with_callbacks,
    record_scheduled_coros,
)


def test_apply_mac_address_first_observation_fires_callback() -> None:
    """A MAC we haven't seen before reaches the controller in canonical form."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    assert monitor.apply_mac_address("kitchen", "94c9601f8cf1") is True
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


def test_apply_mac_address_dedupes_same_value() -> None:
    """Same MAC twice → callback fires once.

    Devices broadcast the same TXT every announce; the dedupe keeps
    DEVICE_UPDATED quiet on a healthy fleet.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


def test_apply_mac_address_fires_on_change() -> None:
    """A different MAC than the last observation re-fires the callback.

    Realistic when an unflashed YAML gets pointed at a different
    physical board mid-test.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    monitor.apply_mac_address("kitchen", "aabbccddeeff")
    assert callbacks.calls == [
        ("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1"),
        ("on_mac_address_change", "kitchen", "AA:BB:CC:DD:EE:FF"),
    ]


def test_apply_mac_address_ignores_empty_string() -> None:
    """Older firmware doesn't broadcast the TXT → empty-string is a no-op.

    The TXT extraction site (``_apply_service_info_to_device``)
    skips ``apply_mac_address`` entirely when the TXT is missing,
    but the apply method still has to drop empty strings on its own
    so callers that read the dict via ``.get("mac") or ""`` don't
    blank out a previously-known MAC.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    assert monitor.apply_mac_address("kitchen", "") is False
    assert callbacks.calls == []


def test_apply_mac_address_unknown_device_is_no_op() -> None:
    """A stray announcement for an unconfigured name does nothing."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    assert monitor.apply_mac_address("not-configured", "94c9601f8cf1") is False
    assert callbacks.calls == []


def test_apply_mac_address_no_op_when_callback_unwired() -> None:
    """A monitor built without ``on_mac_address_change`` is a no-op.

    The callback is optional in :class:`DeviceStateMonitor`'s
    constructor — older callers (in-process usages, smaller test
    fixtures) skip it. ``apply_mac_address`` must short-circuit
    cleanly without dereferencing the ``None`` callback.
    """
    monitor = DeviceStateMonitor(
        get_devices=lambda: [make_device()],
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    assert monitor.apply_mac_address("kitchen", "94c9601f8cf1") is False


# ----------------------------------------------------------------------
# Normalization at ingest
#
# ESPHome firmware broadcasts a lowercase 12-hex-char MAC today, but
# the dashboard normalizes at ingest to ``XX:XX:XX:XX:XX:XX`` so the
# dedupe + persisted sidecar + frontend wire all carry one canonical
# form even if a future firmware switches case or separator style.
# ----------------------------------------------------------------------


def test_apply_mac_address_normalizes_lowercase_to_canonical() -> None:
    """Lowercase 12-hex-char wire form (today's broadcast shape) → uppercase colon-form."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


def test_apply_mac_address_normalizes_uppercase_compact() -> None:
    """Uppercase 12-hex-char input also lands as canonical."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    monitor.apply_mac_address("kitchen", "94C9601F8CF1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


def test_apply_mac_address_already_canonical_is_idempotent() -> None:
    """Already-canonical input passes through unchanged.

    A MAC normalized once shouldn't shift form on the second
    pass — important because the controller stores canonical form
    on the device, and a re-broadcast feeds back through the same
    normalize path.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    monitor.apply_mac_address("kitchen", "94:C9:60:1F:8C:F1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


def test_apply_mac_address_strips_lowercase_colon_separators() -> None:
    """Lowercase colon-separated MAC normalizes to uppercase canonical."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    monitor.apply_mac_address("kitchen", "94:c9:60:1f:8c:f1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


def test_apply_mac_address_strips_dash_separators() -> None:
    """Windows-style ``94-C9-60-...`` normalizes the same way."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    monitor.apply_mac_address("kitchen", "94-C9-60-1F-8C-F1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


def test_apply_mac_address_strips_dot_separators() -> None:
    """Cisco-style ``94c9.601f.8cf1`` normalizes the same way."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    monitor.apply_mac_address("kitchen", "94c9.601f.8cf1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


def test_apply_mac_address_normalized_dedupes_against_stored() -> None:
    """A non-canonical re-broadcast of a stored canonical MAC dedupes.

    The whole point of normalizing at ingest: the dashboard
    shouldn't write a sidecar entry every time the firmware happens
    to switch case style. The dedupe is keyed off the canonical
    form so equivalence holds across surface formats.
    """
    devices = [make_device(mac_address="94:C9:60:1F:8C:F1")]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)
    # Wire form (lowercase 12-hex) — what the firmware actually broadcasts.
    assert monitor.apply_mac_address("kitchen", "94c9601f8cf1") is False
    # Dash-separated — what some vendored tools might use.
    assert monitor.apply_mac_address("kitchen", "94-C9-60-1F-8C-F1") is False
    assert callbacks.calls == []


def test_apply_mac_address_rejects_non_hex_input() -> None:
    """Garbage TXT content is dropped, not stored."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    assert monitor.apply_mac_address("kitchen", "not-a-mac") is False
    assert callbacks.calls == []


def test_apply_mac_address_rejects_wrong_length() -> None:
    """Too-short / too-long values are dropped (not silently truncated)."""
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    # 11 chars
    assert monitor.apply_mac_address("kitchen", "94c9601f8cf") is False
    # 13 chars
    assert monitor.apply_mac_address("kitchen", "94c9601f8cf1a") is False
    assert callbacks.calls == []


def test_apply_mac_address_rejects_correct_length_non_hex() -> None:
    """A 12-char-after-stripping value with non-hex chars is dropped.

    The length check passes (12 chars) but ``int(_, 16)`` raises
    ``ValueError``; ``_normalize_mac`` catches and returns empty so
    a corrupt TXT can't pollute the sidecar with a value that
    looks the right shape but doesn't decode to a real MAC.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([make_device()])
    # 12 chars, but the 'Z' isn't valid hex.
    assert monitor.apply_mac_address("kitchen", "94c9601f8cZZ") is False
    assert callbacks.calls == []


def test_apply_mac_address_refires_after_device_rebuild() -> None:
    """A rebuilt Device with empty MAC gets repopulated by the next mDNS event.

    Atomic-write editor races (vscode-on-macOS et al.) can briefly
    REMOVE+re-ADD a device with ``previous=None``, leaving the new
    Device with ``mac_address=""``. The monitor's dedupe is keyed off
    the device's own field, so the next mDNS announcement should
    repopulate without short-circuiting on a stale cache.
    """
    devices = [make_device(mac_address="94:C9:60:1F:8C:F1")]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    # Steady state: the device already has the canonical MAC, so a
    # repeat broadcast (in the wire form) is a no-op.
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    assert callbacks.calls == []

    # Atomic-save churn rebuilds the Device with empty fields. The
    # next mDNS announcement should write the MAC back through the
    # callback, in canonical form.
    devices[0].mac_address = ""
    monitor.apply_mac_address("kitchen", "94c9601f8cf1")
    assert callbacks.calls == [("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")]


# ----------------------------------------------------------------------
# DevicesController._on_mac_address_change — full pipe + persistence
#
# The controller-level callback receives the *already normalized*
# MAC (the monitor's ``apply_mac_address`` does the normalization
# before invoking the change callback), so these tests pass the
# canonical form directly.
# ----------------------------------------------------------------------


def _device_kitchen(**overrides: Any) -> Device:
    return make_device(address="", **overrides)


def test_on_mac_address_change_updates_device_and_fires_event() -> None:
    """Full pipe: callback writes the MAC + fires DEVICE_UPDATED."""
    device = _device_kitchen()
    scheduled: list[object] = []
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94:C9:60:1F:8C:F1")

    assert device.mac_address == "94:C9:60:1F:8C:F1"
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)


def test_on_mac_address_change_persists_to_sidecar() -> None:
    """First observation schedules exactly one sidecar write."""
    device = _device_kitchen()
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94:C9:60:1F:8C:F1")

    assert len(scheduled) == 1


def test_on_mac_address_change_skips_persist_when_unchanged() -> None:
    """Repeat observation of the same MAC must not schedule any I/O.

    mDNS announces the same TXT every cycle on a healthy fleet; a
    naive write-through would hammer the sidecar on every announce.
    The dedupe is keyed off ``device.mac_address`` so a steady-state
    broadcast short-circuits before either the in-memory write or
    the executor-bound ``set_device_metadata`` call.
    """
    device = _device_kitchen(mac_address="94:C9:60:1F:8C:F1")
    scheduled: list[object] = []
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94:C9:60:1F:8C:F1")

    assert scheduled == []
    assert captured == []


def test_on_mac_address_change_unknown_device_is_noop() -> None:
    """Stray callback for an unconfigured name doesn't raise or fire events."""
    scheduled: list[object] = []
    controller, captured = make_devices_controller_with_bus(
        [], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("ghost", "94:C9:60:1F:8C:F1")

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
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94:C9:60:1F:8C:F0")

    assert device.mac_address == "94:C9:60:1F:8C:F0"
    assert device.ethernet_mac == "94:C9:60:1F:8C:F3"
    assert device.bluetooth_mac == ""


def test_on_mac_address_change_derives_bluetooth_mac_on_esp32() -> None:
    """ESP32 + bluetooth integration → ``bluetooth_mac`` = primary + 2."""
    device = _device_kitchen(
        target_platform="esp32",
        loaded_integrations=["api", "wifi", "esp32_ble_tracker"],
    )
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94:C9:60:1F:8C:F0")

    assert device.mac_address == "94:C9:60:1F:8C:F0"
    assert device.ethernet_mac == ""
    assert device.bluetooth_mac == "94:C9:60:1F:8C:F2"


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
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94:C9:60:1F:8C:F0")

    assert device.mac_address == "94:C9:60:1F:8C:F0"
    assert device.ethernet_mac == "94:C9:60:1F:8C:F0"
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
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94:C9:60:1F:8C:F0")

    assert device.mac_address == "94:C9:60:1F:8C:F0"
    assert device.ethernet_mac == ""
    assert device.bluetooth_mac == ""


def test_on_mac_address_change_clears_stale_derived_macs_on_change() -> None:
    """A new primary MAC overwrites previously-derived ethernet/bluetooth.

    A device whose YAML drops the ``ethernet`` integration after
    a re-flash would otherwise carry a stale ``ethernet_mac`` until
    the next reload. Recomputing on every change ensures the
    derived fields can never lag behind the integration loadout.
    """
    device = _device_kitchen(
        target_platform="esp32",
        loaded_integrations=["api", "wifi"],  # no ethernet, no bluetooth
        mac_address="94:C9:60:1F:8C:00",
        ethernet_mac="94:C9:60:1F:8C:03",  # stale from a prior loadout
        bluetooth_mac="94:C9:60:1F:8C:02",
    )
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "94:C9:60:1F:8C:F0")

    assert device.mac_address == "94:C9:60:1F:8C:F0"
    assert device.ethernet_mac == ""
    assert device.bluetooth_mac == ""


def test_on_mac_address_change_rederives_ethernet_and_bluetooth_when_primary_changes() -> None:
    """A new primary on an ESP32 with ethernet + bluetooth re-derives both.

    Pins the inverse of the "clears stale" case: when the
    integrations *are* still loaded, the derived MACs must shift
    to track the new base. A factory replacement (same YAML, new
    physical board) is the realistic trigger — the broadcast MAC
    changes and every interface MAC has to follow.
    """
    device = _device_kitchen(
        target_platform="esp32",
        loaded_integrations=["api", "wifi", "ethernet", "esp32_ble_tracker"],
        mac_address="94:C9:60:1F:8C:00",
        ethernet_mac="94:C9:60:1F:8C:03",  # base + 3 from the old primary
        bluetooth_mac="94:C9:60:1F:8C:02",  # base + 2 from the old primary
    )
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_mac_address_change("kitchen", "AA:BB:CC:DD:EE:F0")

    assert device.mac_address == "AA:BB:CC:DD:EE:F0"
    assert device.ethernet_mac == "AA:BB:CC:DD:EE:F3"
    assert device.bluetooth_mac == "AA:BB:CC:DD:EE:F2"
