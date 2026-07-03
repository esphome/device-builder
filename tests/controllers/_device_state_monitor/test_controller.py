"""Tests for the DeviceStateMonitor controller."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor.controller import DeviceStateMonitor


def _device(configuration: str, *, queued_update: bool = False) -> SimpleNamespace:
    """Minimal stand-in for a Device carrying just the fields apply_queued_update reads."""
    return SimpleNamespace(configuration=configuration, queued_update=queued_update)


@pytest.fixture
def monitor():
    """Fixture to provide a mock DeviceStateMonitor."""
    # Create the callback mock
    mock_on_queued = MagicMock()

    return DeviceStateMonitor(
        get_devices=MagicMock(return_value=[]),
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_queued_update_change=mock_on_queued,  # Pass the callback here
    )


def test_apply_queued_update(monitor):
    """Test that apply_queued_update triggers the callback correctly."""
    device_name = "test_device"
    configuration = "test_device.yaml"

    # apply_queued_update is keyed on configuration — the unique per-device
    # key — rather than the mDNS-style name fan-out, so it resolves the
    # single matching device via _find_device_by_configuration instead of
    # _any_matching_device_differs.
    monitor._find_device_by_configuration = MagicMock(
        return_value=_device(configuration, queued_update=False)
    )

    # 1. Test setting to True
    result = monitor.apply_queued_update(device_name, configuration, is_queued=True)

    # Verify the method returned True (indicating a change occurred)
    assert result is True
    # Verify the callback was fired with (name, is_queued, configuration)
    monitor._on_queued_update_change.assert_called_with(device_name, True, configuration)

    # 2. Test setting to False
    monitor._find_device_by_configuration = MagicMock(
        return_value=_device(configuration, queued_update=True)
    )
    result = monitor.apply_queued_update(device_name, configuration, is_queued=False)

    assert result is True
    monitor._on_queued_update_change.assert_called_with(device_name, False, configuration)


def test_apply_queued_update_missing_callback():
    """Test early return if no callback is registered."""
    monitor = DeviceStateMonitor(
        get_devices=MagicMock(),
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_queued_update_change=None,  # Callback omitted
    )
    assert monitor.apply_queued_update("kitchen", "kitchen.yaml", is_queued=True) is False


def test_apply_queued_update_no_diff(monitor):
    """Test early return if the device already carries the requested flag."""
    configuration = "kitchen.yaml"
    monitor._find_device_by_configuration = MagicMock(
        return_value=_device(configuration, queued_update=True)
    )
    monitor._on_queued_update_change = MagicMock()

    assert monitor.apply_queued_update("kitchen", configuration, is_queued=True) is False
    monitor._on_queued_update_change.assert_not_called()


def test_apply_queued_update_no_matching_device(monitor):
    """Test early return when no device matches the given configuration.

    Regression guard for the sibling-collision bug this signature change
    fixed: a stray or superseded configuration must not fall back to a
    name-keyed fan-out that could touch an unrelated device.
    """
    monitor._find_device_by_configuration = MagicMock(return_value=None)
    monitor._on_queued_update_change = MagicMock()

    assert monitor.apply_queued_update("kitchen", "missing.yaml", is_queued=True) is False
    monitor._on_queued_update_change.assert_not_called()


def test_apply_queued_update_triggers_callback(monitor):
    """Test standard execution when the state differs."""
    configuration = "kitchen.yaml"
    monitor._find_device_by_configuration = MagicMock(
        return_value=_device(configuration, queued_update=False)
    )
    monitor._on_queued_update_change = MagicMock()

    assert monitor.apply_queued_update("kitchen", configuration, is_queued=True) is True
    monitor._on_queued_update_change.assert_called_once_with("kitchen", True, configuration)
