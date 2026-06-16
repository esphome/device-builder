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
