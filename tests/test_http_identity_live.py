"""Tests for ``Device.runtime_state.http_identity_live`` tracking."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import EventType

from .conftest import (
    close_scheduled_coro,
    make_device,
    make_devices_controller_with_bus,
    make_state_monitor_with_callbacks,
    stub_async_service_info,
)


def _device(**overrides: Any) -> Any:
    base: dict[str, Any] = {"api_enabled": False, "loaded_integrations": ["mqtt", "wifi"]}
    base.update(overrides)
    return make_device(**base)


# ─── Controller handler ───────────────────────────────────────────


async def test_on_http_identity_live_change_updates_device_fires_event_not_persisted() -> None:
    """The handler writes the flag, fires DEVICE_UPDATED, and does not persist."""
    device = _device()
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=close_scheduled_coro
    )

    controller._on_http_identity_live_change("kitchen", live=True)

    assert device.runtime_state.http_identity_live is True
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)
    # Runtime-only: identity freshness must not write the metadata sidecar.
    assert "http_identity_live" not in (controller._metadata_store.get(device.configuration) or {})


async def test_on_http_identity_live_change_skips_when_same() -> None:
    """No-op (no event) when the device already carries the value."""
    device = _device(http_identity_live=True)
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=close_scheduled_coro
    )

    controller._on_http_identity_live_change("kitchen", live=True)

    assert captured == []


# ─── Monitor apply ────────────────────────────────────────────────


def test_apply_http_identity_live_forwards_then_dedupes() -> None:
    """The first flip forwards; a repeat of the same value stays quiet."""
    device = _device()
    monitor, callbacks = make_state_monitor_with_callbacks([device])

    assert monitor.apply_http_identity_live("kitchen", live=True) is True
    assert device.runtime_state.http_identity_live is True
    assert monitor.apply_http_identity_live("kitchen", live=True) is False
    assert monitor.apply_http_identity_live("kitchen", live=False) is True

    assert callbacks.calls_for("on_http_identity_live_change") == [
        ("on_http_identity_live_change", "kitchen", True),
        ("on_http_identity_live_change", "kitchen", False),
    ]


def test_apply_http_identity_live_unwired_is_a_noop() -> None:
    """A monitor built without the callback never forwards."""
    device = _device()
    monitor = DeviceStateMonitor(
        get_devices=lambda: [device],
        on_state_change=lambda *_a: None,
        on_ip_change=lambda *_a: None,
    )

    assert monitor.apply_http_identity_live("kitchen", live=True) is False
    assert device.runtime_state.http_identity_live is False


# ─── verify_http_identity ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("resolved", "props", "expected"),
    [
        pytest.param(False, {}, False, id="confirmed_miss_clears"),
        pytest.param(True, {"version": "2026.8.0"}, True, id="identity_bearing_keeps"),
        pytest.param(True, {"path": "/"}, False, id="identity_less_clears"),
    ],
)
async def test_verify_http_identity_verdicts(
    monkeypatch: pytest.MonkeyPatch, resolved: bool, props: dict[str, str], expected: bool
) -> None:
    """Only a confirmed miss or an identity-less answer clears the flag."""
    device = _device(http_identity_live=True)
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.mdns._zeroconf = MagicMock()
    stub_async_service_info(monkeypatch, resolved=resolved, properties=props)

    await monitor.mdns.verify_http_identity("kitchen")

    assert device.runtime_state.http_identity_live is expected


async def test_verify_inflight_resolve_leaves_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """No verdict (a resolve already in flight) never demotes."""
    device = _device(http_identity_live=True)
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.mdns._zeroconf = MagicMock()
    info = stub_async_service_info(monkeypatch, properties={})
    monitor.mdns._inflight_resolves.add(info.name)

    await monitor.mdns.verify_http_identity("kitchen")

    assert device.runtime_state.http_identity_live is True
    assert callbacks.calls_for("on_http_identity_live_change") == []


async def test_verify_without_zeroconf_is_a_noop() -> None:
    device = _device(http_identity_live=True)
    monitor, callbacks = make_state_monitor_with_callbacks([device])
    monitor.mdns._zeroconf = None

    await monitor.mdns.verify_http_identity("kitchen")

    assert device.runtime_state.http_identity_live is True
    assert callbacks.calls_for("on_http_identity_live_change") == []
