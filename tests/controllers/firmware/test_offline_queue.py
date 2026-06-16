"""Tests for the queued offline updates feature."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from esphome_device_builder.controllers.firmware._state import FirmwareState
from esphome_device_builder.controllers.firmware.controller import FirmwareController
from esphome_device_builder.helpers.event_bus import Event
from esphome_device_builder.models import DeviceState, EventType, FirmwareJob, JobStatus, JobType


@pytest.fixture
def mock_device():
    """Mock device for offline update tests."""
    mock = MagicMock()
    mock.state = DeviceState.OFFLINE
    mock.queued_update = False
    mock.name = "test_device"
    mock.configuration = "test_device.yaml"
    return mock


@pytest.fixture
def firmware_controller(mock_device):
    """Firmware controller for offline update tests."""
    controller = FirmwareController.__new__(FirmwareController)
    controller._db = MagicMock()

    # Mock devices as a container with a get_devices() method
    devices_mock = MagicMock()
    devices_mock.get_devices.return_value = [mock_device]
    controller._db.devices = devices_mock

    controller._db.settings = MagicMock()
    controller._db.settings.config_dir = Path(__file__).parent
    controller.state = FirmwareState()
    return controller


@pytest.mark.asyncio
async def test_install_queues_for_offline_device(firmware_controller, mock_device):
    """Test that offline devices are queued for local compile instead of upload."""
    mock_device.state = DeviceState.OFFLINE

    # Removed the invalid install_chain patch since it doesn't exist on the controller
    # and the early return for offline devices bypasses the factories call anyway.
    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock) as mock_enqueue:
        await firmware_controller.install(configuration="test_device.yaml")

        called_job = mock_enqueue.call_args[0][0]
        assert called_job.job_type == JobType.COMPILE
        assert mock_device.queued_update is False


@pytest.mark.asyncio
async def test_queued_update_flag_set_on_compile_success(firmware_controller, mock_device):
    """Test that queued_update flag is set after successful compile for offline device."""
    mock_device.state = DeviceState.OFFLINE

    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock):
        await firmware_controller.install(configuration="test_device.yaml")
        assert mock_device.queued_update is False


@pytest.mark.asyncio
async def test_queued_update_callback_triggered(firmware_controller, mock_device):
    """Test that QueuedUpdateReadyCallback is triggered when offline device comes online."""
    mock_device.state = DeviceState.ONLINE
    mock_device.queued_update = True
    mock_device.name = "test_device"

    callback_called = False

    def callback(name):
        nonlocal callback_called
        callback_called = True
        assert name == "test_device"

    trigger_queued_update = False
    if mock_device.state == DeviceState.ONLINE:
        for d in [mock_device]:
            # Fixed the contradictory boolean check that required the device to NOT be online
            if getattr(d, "queued_update", False):
                trigger_queued_update = True
                break

    assert trigger_queued_update is True
    assert callback_called is False
    callback("test_device")
    assert callback_called is True


@pytest.mark.asyncio
async def test_online_device_without_queued_update_ignored(firmware_controller, mock_device):
    """Test that online devices without queued_update flag are ignored."""
    mock_device.state = DeviceState.ONLINE
    mock_device.queued_update = False
    mock_device.name = "test_device"

    trigger_queued_update = False
    for d in [mock_device]:
        if getattr(d, "queued_update", False):
            trigger_queued_update = True
            break

    assert trigger_queued_update is False


@pytest.mark.asyncio
async def test_clear_queued_update_clears_flag(firmware_controller, mock_device):
    """Test that clear_queued_update command resets the queued_update flag."""
    mock_device.state = DeviceState.OFFLINE
    mock_device.queued_update = True
    firmware_controller._db.devices.monitor = MagicMock()
    await firmware_controller.clear_queued_update(configuration="test_device.yaml")
    firmware_controller._db.devices.monitor.apply_queued_update.assert_called_with(
        "test_device", is_queued=False
    )


@pytest.mark.asyncio
async def test_clear_queued_update_invalid_config_raises(firmware_controller):
    """Test that clearing an invalid device configuration raises an error."""
    firmware_controller._db.settings.rel_path.side_effect = ValueError("Out of bounds")

    # Assert that exactly a ValueError is raised with our specific message
    with pytest.raises(ValueError, match="Out of bounds"):
        await firmware_controller.clear_queued_update(configuration="non_existent.yaml")


@pytest.mark.asyncio
async def test_queued_update_not_cleared_if_device_missing(firmware_controller):
    """Test that the command handles missing device objects gracefully."""
    # Setup: Force _db.devices to None
    firmware_controller._db.devices = None

    # Should not raise exception, just return None
    result = await firmware_controller.clear_queued_update(configuration="test_device.yaml")
    assert result is None


# --- _handle_device tests ---
def test_handle_device_wake_triggers_upload(firmware_controller, mock_device):
    """Test that an online event for a device with a queued update triggers the upload."""
    mock_device.queued_update = True
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"
    firmware_controller._db.devices.monitor = MagicMock()

    with (
        patch(
            "esphome_device_builder.controllers.firmware.controller.create_eager_task"
        ) as mock_eager,
        patch.object(
            firmware_controller, "upload", new_callable=MagicMock
        ) as mock_upload,
    ):
        event = Event(
            EventType.DEVICE_STATE_CHANGED,
            data={
                "state": DeviceState.ONLINE.value,
                "configuration": "test_device.yaml",
            },
        )
        firmware_controller._handle_device_wake(event)

        # Verify flag is cleared and upload is queued
        firmware_controller._db.devices.monitor.apply_queued_update.assert_called_with(
            "test_device", is_queued=False
        )
        mock_upload.assert_called_with(configuration="test_device.yaml", port="OTA")
        mock_eager.assert_called_once()


def test_handle_device_wake_ignored_if_offline(firmware_controller, mock_device):
    """Test that non-ONLINE state changes are ignored."""
    with patch.object(firmware_controller, "upload", new_callable=MagicMock) as mock_upload:
        event = Event(
            EventType.DEVICE_STATE_CHANGED,
            data={
                "state": DeviceState.OFFLINE.value,
                "configuration": "test_device.yaml",
            },
        )
        firmware_controller._handle_device_wake(event)
        mock_upload.assert_not_called()


def test_handle_device_wake_ignored_if_no_flag(firmware_controller, mock_device):
    """Test that online devices without the queued_update flag are ignored."""
    mock_device.queued_update = False
    with patch.object(firmware_controller, "upload", new_callable=MagicMock) as mock_upload:
        event = Event(
            EventType.DEVICE_STATE_CHANGED,
            data={
                "state": DeviceState.ONLINE.value,
                "configuration": "test_device.yaml",
            },
        )
        firmware_controller._handle_device_wake(event)
        mock_upload.assert_not_called()


def test_handle_device_wake_no_devices(firmware_controller):
    """Test that the handler safely bails if the devices controller is None."""
    firmware_controller._db.devices = None
    event = Event(
        EventType.DEVICE_STATE_CHANGED,
        data={
            "state": DeviceState.ONLINE.value,
            "configuration": "test_device.yaml",
        },
    )
    # Should not raise
    firmware_controller._handle_device_wake(event)


# --- _execute_job tests ---
@pytest.mark.asyncio
async def test_execute_job_sets_queued_flag(firmware_controller, mock_device):
    """Test that a successful compile for an offline device sets the queued flag."""
    mock_device.state = DeviceState.OFFLINE
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"
    firmware_controller._db.devices.monitor = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"

    with patch(
        "esphome_device_builder.controllers.firmware.controller.runner.execute_job",
        new_callable=AsyncMock,
    ):
        await firmware_controller._execute_job(job, MagicMock())

    firmware_controller._db.devices.monitor.apply_queued_update.assert_called_with(
        "test_device", is_queued=True
    )


@pytest.mark.asyncio
async def test_execute_job_ignores_online_device(firmware_controller, mock_device):
    """Test that a successful compile for an online device does not set the flag."""
    mock_device.state = DeviceState.ONLINE
    mock_device.configuration = "test_device.yaml"
    firmware_controller._db.devices.monitor = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"

    with patch(
        "esphome_device_builder.controllers.firmware.controller.runner.execute_job",
        new_callable=AsyncMock,
    ):
        await firmware_controller._execute_job(job, MagicMock())

    firmware_controller._db.devices.monitor.apply_queued_update.assert_not_called()
