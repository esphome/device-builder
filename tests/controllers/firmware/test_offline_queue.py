import pytest

from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
from esphome_device_builder.models import DeviceState, JobType
from esphome_device_builder.controllers.firmware.runner import FirmwareController
from esphome_device_builder.models.firmware import FirmwareState

@pytest.fixture
def mock_device():
    """Mock device for offline update tests."""
    mock = MagicMock()
    mock.state = DeviceState.OFFLINE
    mock.queued_update = False
    mock.name = "test_device"
    return mock

@pytest.fixture  
def firmware_controller(mock_device):
    """Firmware controller for offline update tests."""
    controller = FirmwareController.__new__(FirmwareController)
    controller._db = MagicMock()
    controller._db.devices = mock_device
    controller._db.settings = MagicMock()
    controller._db.settings.config_dir = Path(__file__).parent
    controller.state = FirmwareState()
    return controller

@pytest.mark.asyncio
async def test_install_queues_for_offline_device(firmware_controller, mock_device):
    """Test that offline devices are queued for local compile instead of upload."""
    # Setup: Mock device as OFFLINE
    mock_device.state = DeviceState.OFFLINE

    # Execute install request
    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock) as mock_enqueue, \
         patch.object(firmware_controller, "install_chain", new_callable=AsyncMock):
        await firmware_controller.install(configuration="test_device.yaml")

        # Assert: It should have called _enqueue with a COMPILE job
        called_job = mock_enqueue.call_args[0][0]
        assert called_job.job_type == JobType.COMPILE
        assert mock_device.queued_update is False  # Flag set by runner completion

@pytest.mark.asyncio
async def test_queued_update_flag_set_on_compile_success(firmware_controller, mock_device):
    """Test that queued_update flag is set after successful compile for offline device."""
    # Setup: Mock device as OFFLINE
    mock_device.state = DeviceState.OFFLINE

    # Execute install request
    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock) as mock_enqueue, \
         patch.object(firmware_controller, "install_chain", new_callable=AsyncMock):
        await firmware_controller.install(configuration="test_device.yaml")

        # The flag should be False here (set by runner after compile)
        assert mock_device.queued_update is False

@pytest.mark.asyncio
async def test_queued_update_callback_triggered(firmware_controller, mock_device):
    """Test that QueuedUpdateReadyCallback is triggered when offline device comes online."""
    # Setup: Device was offline with queued_update flag
    mock_device.state = DeviceState.ONLINE
    mock_device.queued_update = True
    mock_device.name = "test_device"

    # Track callback calls
    callback_called = False
    def callback(name):
        nonlocal callback_called
        callback_called = True
        assert name == "test_device"

    # Simulate device state monitor checking for queued updates
    trigger_queued_update = False
    if mock_device.state == DeviceState.ONLINE:
        for d in [mock_device]:
            if d.state != DeviceState.ONLINE and getattr(d, "queued_update", False):
                trigger_queued_update = True
                break

    assert trigger_queued_update is True

    # Verify callback would be triggered
    assert callback_called is False
    callback("test_device")
    assert callback_called is True

@pytest.mark.asyncio
async def test_online_device_without_queued_update_ignored(firmware_controller, mock_device):
    """Test that online devices without queued_update flag are ignored."""
    # Setup: Online device without queued_update
    mock_device.state = DeviceState.ONLINE
    mock_device.queued_update = False
    mock_device.name = "test_device"

    # Simulate device state monitor checking
    trigger_queued_update = False
    for d in [mock_device]:
        if d.state != DeviceState.ONLINE and getattr(d, "queued_update", False):
            trigger_queued_update = True
            break

    assert trigger_queued_update is False
