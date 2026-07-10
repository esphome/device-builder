"""Tests for ``Device.runtime_state.active_source`` tracking on the device snapshot."""

from __future__ import annotations

from typing import Any

from esphome_device_builder.models import DeviceState, EventType, ReachabilitySource

from .conftest import (
    close_scheduled_coro,
    make_device,
    make_devices_controller_with_bus,
    make_state_monitor_with_callbacks,
)


def _device(**overrides: Any) -> Any:
    return make_device(**overrides)


# ─── Controller handler ───────────────────────────────────────────


async def test_on_source_change_updates_device_fires_event_not_persisted() -> None:
    """The handler writes ``active_source``, fires DEVICE_UPDATED, and does not persist."""
    device = _device()
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=close_scheduled_coro
    )

    controller._on_source_change("kitchen", ReachabilitySource.MDNS)

    assert device.runtime_state.active_source == ReachabilitySource.MDNS
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)
    # Runtime-only: a reachability flip must not write the metadata sidecar.
    assert "active_source" not in (controller._metadata_store.get(device.configuration) or {})


async def test_on_source_change_skips_when_same() -> None:
    """No-op (no event) when the device already carries the source."""
    device = _device()
    device.runtime_state.active_source = ReachabilitySource.MDNS
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=close_scheduled_coro
    )

    controller._on_source_change("kitchen", ReachabilitySource.MDNS)

    assert captured == []


# ─── Monitor emission ─────────────────────────────────────────────


def test_apply_mdns_sets_active_source_then_dedupes() -> None:
    """An mDNS claim flips the source to mDNS once; a re-announce stays quiet."""
    device = make_device(state=DeviceState.UNKNOWN)
    monitor, callbacks = make_state_monitor_with_callbacks([device])

    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)

    assert device.runtime_state.active_source == ReachabilitySource.MDNS
    assert callbacks.calls_for("on_source_change") == [
        ("on_source_change", "kitchen", ReachabilitySource.MDNS)
    ]

    # Re-announce of the same online+mdns state: the source ledger is unchanged,
    # so no redundant source-change callback fires.
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)

    assert len(callbacks.calls_for("on_source_change")) == 1


def test_forget_resets_active_source_to_unknown() -> None:
    """Dropping the ledger entry (mDNS ``Removed``) flips the source to UNKNOWN."""
    device = make_device(state=DeviceState.UNKNOWN)
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)

    monitor.forget("kitchen")

    assert device.runtime_state.active_source == ReachabilitySource.UNKNOWN
    assert callbacks.calls_for("on_source_change")[-1] == (
        "on_source_change",
        "kitchen",
        ReachabilitySource.UNKNOWN,
    )


def test_ping_takes_over_after_mdns_goes_dark() -> None:
    """The realistic dark transition: mDNS OFFLINE + forget, then ping revives it."""
    device = make_device(state=DeviceState.UNKNOWN)
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)

    # mDNS TTL expiry: the browser marks it OFFLINE via mdns, then forgets it.
    monitor.apply("kitchen", DeviceState.OFFLINE, "mdns")
    monitor.forget("kitchen")
    assert device.runtime_state.active_source == ReachabilitySource.UNKNOWN

    # Ping still reaches it → ping becomes the authoritative source, so the UI
    # knows the mDNS-sourced deployed_* values are no longer trustworthy.
    monitor.apply("kitchen", DeviceState.ONLINE, "ping")

    assert device.runtime_state.active_source == ReachabilitySource.PING
