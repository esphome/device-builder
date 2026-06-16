"""Tests for the DeviceStateMonitor controller."""

from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor.controller import DeviceStateMonitor


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

    # We must mock _any_matching_device_differs because it checks device state
    # to decide if the change is "real" or redundant.
    monitor._any_matching_device_differs = MagicMock(return_value=True)

    # 1. Test setting to True
    result = monitor.apply_queued_update(device_name, is_queued=True)

    # Verify the method returned True (indicating a change occurred)
    assert result is True
    # Verify the callback was fired
    monitor._on_queued_update_change.assert_called_with(device_name, True)

    # 2. Test setting to False
    result = monitor.apply_queued_update(device_name, is_queued=False)

    assert result is True
    monitor._on_queued_update_change.assert_called_with(device_name, False)


def test_apply_queued_update_missing_callback():
    """Test early return if no callback is registered."""
    monitor = DeviceStateMonitor(
        get_devices=MagicMock(),
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_queued_update_change=None,  # Callback omitted
    )
    assert monitor.apply_queued_update("kitchen", is_queued=True) is False


def test_apply_queued_update_no_diff(monitor):
    """Test early return if the device state already matches."""
    monitor._any_matching_device_differs = MagicMock(return_value=False)
    monitor._on_queued_update_change = MagicMock()

    assert monitor.apply_queued_update("kitchen", is_queued=True) is False
    monitor._on_queued_update_change.assert_not_called()


def test_apply_queued_update_triggers_callback(monitor):
    """Test standard execution when the state differs."""
    monitor._any_matching_device_differs = MagicMock(return_value=True)
    monitor._on_queued_update_change = MagicMock()

    assert monitor.apply_queued_update("kitchen", is_queued=True) is True
    monitor._on_queued_update_change.assert_called_once_with("kitchen", True)
