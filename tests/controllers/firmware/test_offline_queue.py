"""Tests for the queued offline updates feature."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from esphome_device_builder.controllers.devices.controller import DevicesController
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

    # Mock devices as a container with both get_devices() and the new get_by_configuration()
    devices_mock = MagicMock()
    devices_mock.get_devices.return_value = [mock_device]
    devices_mock.get_by_configuration.side_effect = lambda c: (
        mock_device if c == mock_device.configuration else None
    )
    controller._db.devices = devices_mock

    controller._db.settings = MagicMock()
    controller._db.settings.config_dir = Path(__file__).parent
    controller.state = FirmwareState()
    controller._armed_deferred_installs = set()
    return controller


@pytest.mark.asyncio
async def test_install_queues_deferred_compile_for_offline_device(firmware_controller, mock_device):
    """Test that offline devices queue a COMPILE job marked as a deferred install."""
    mock_device.state = DeviceState.OFFLINE

    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock) as mock_enqueue:
        await firmware_controller.install(configuration="test_device.yaml")

        called_job = mock_enqueue.call_args[0][0]
        assert called_job.job_type == JobType.COMPILE
        assert getattr(called_job, "is_deferred_install", False) is True


@pytest.mark.asyncio
async def test_compile_does_not_mark_deferred(firmware_controller, mock_device):
    """Test that a plain compile does NOT mark the job as a deferred install."""
    mock_device.state = DeviceState.OFFLINE

    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock) as mock_enqueue:
        await firmware_controller.compile(configuration="test_device.yaml")

        called_job = mock_enqueue.call_args[0][0]
        assert called_job.job_type == JobType.COMPILE
        assert getattr(called_job, "is_deferred_install", False) is False


@pytest.mark.asyncio
async def test_clear_queued_update_clears_flag(firmware_controller, mock_device):
    """Test that clear_queued_update command resets the queued_update flag."""
    mock_device.state = DeviceState.OFFLINE
    mock_device.queued_update = True
    firmware_controller._armed_deferred_installs.add("test_device.yaml")

    firmware_controller._db.devices.set_queued_update = MagicMock()

    await firmware_controller.clear_queued_update(configuration="test_device.yaml")

    firmware_controller._db.devices.set_queued_update.assert_called_with(
        "test_device", is_queued=False
    )
    assert "test_device.yaml" not in firmware_controller._armed_deferred_installs


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
    firmware_controller._armed_deferred_installs.add("test_device.yaml")

    # Should not raise exception, just return None
    result = await firmware_controller.clear_queued_update(configuration="test_device.yaml")
    assert result is None


def test_on_queued_update_change_matches_device(mock_device):
    """Test that a matching device is updated, persisted, and an event is fired."""
    # Initialize a mock DevicesController without running __init__
    controller = DevicesController.__new__(DevicesController)
    controller.get_devices = MagicMock(return_value=[mock_device])
    controller._metadata_store = MagicMock()
    controller._fire_device_updated = MagicMock()

    controller._on_queued_update_change("test_device", True)

    # Verify the device flag was updated
    assert mock_device.queued_update is True

    # Verify the metadata store was instructed to persist the change
    controller._metadata_store.update.assert_called_once_with(
        "test_device.yaml", queued_update=True
    )

    # Verify the updated event was fired for the UI/bus
    controller._fire_device_updated.assert_called_once_with(mock_device)


def test_on_queued_update_change_skips_unmatched(mock_device):
    """Test that devices with a different name are skipped."""
    controller = DevicesController.__new__(DevicesController)
    controller.get_devices = MagicMock(return_value=[mock_device])
    controller._metadata_store = MagicMock()
    controller._fire_device_updated = MagicMock()

    controller._on_queued_update_change("other_device", True)

    # Verify the device flag remains untouched
    assert mock_device.queued_update is False

    # Verify persistence and events were not triggered
    controller._metadata_store.update.assert_not_called()
    controller._fire_device_updated.assert_not_called()


# --- _rehydrate_armed_deferred_installs tests ---
def test_rehydrate_armed_deferred_installs_arms_queued_devices(firmware_controller):
    """Devices with a persisted queued_update are added to the armed set."""
    queued_device = MagicMock(configuration="kitchen.yaml", queued_update=True)
    not_queued_device = MagicMock(configuration="living_room.yaml", queued_update=False)
    firmware_controller._db.devices.get_devices.return_value = [
        queued_device,
        not_queued_device,
    ]

    firmware_controller._rehydrate_armed_deferred_installs()

    assert firmware_controller._armed_deferred_installs == {"kitchen.yaml"}


def test_rehydrate_armed_deferred_installs_handles_no_devices_controller(firmware_controller):
    """A None devices controller (e.g. unexpected boot ordering) must not raise."""
    firmware_controller._db.devices = None

    firmware_controller._rehydrate_armed_deferred_installs()

    assert firmware_controller._armed_deferred_installs == set()


def test_rehydrate_armed_deferred_installs_handles_empty_device_list(firmware_controller):
    """No devices at all is a no-op, not an error."""
    firmware_controller._db.devices.get_devices.return_value = []

    firmware_controller._rehydrate_armed_deferred_installs()

    assert firmware_controller._armed_deferred_installs == set()


def test_rehydrate_armed_deferred_installs_preserves_existing_entries(firmware_controller):
    """Rehydration adds to, rather than replaces, any pre-existing armed entries."""
    firmware_controller._armed_deferred_installs.add("already_armed.yaml")
    queued_device = MagicMock(configuration="kitchen.yaml", queued_update=True)
    firmware_controller._db.devices.get_devices.return_value = [queued_device]

    firmware_controller._rehydrate_armed_deferred_installs()

    assert firmware_controller._armed_deferred_installs == {
        "already_armed.yaml",
        "kitchen.yaml",
    }


# --- _handle_device tests ---
def test_handle_device_wake_triggers_upload(firmware_controller, mock_device):
    """Test that an online event for a device with a queued update triggers the upload."""
    mock_device.queued_update = True
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"

    firmware_controller._db.devices.set_queued_update = MagicMock()
    firmware_controller._armed_deferred_installs.add("test_device.yaml")

    with patch.object(firmware_controller, "upload", new_callable=MagicMock) as mock_upload:
        event = Event(
            EventType.DEVICE_STATE_CHANGED,
            data={
                "state": DeviceState.ONLINE.value,
                "configuration": "test_device.yaml",
            },
        )
        firmware_controller._handle_device_wake(event)

        firmware_controller._db.devices.set_queued_update.assert_not_called()
        mock_upload.assert_called_with(configuration="test_device.yaml", port="OTA")
        firmware_controller._db.create_background_task.assert_called_once()
        # Disarmed immediately at dispatch — a flap mid-flash must not re-enter.
        assert "test_device.yaml" not in firmware_controller._armed_deferred_installs


def test_handle_device_wake_ignored_if_offline(firmware_controller, mock_device):
    """Test that non-ONLINE state changes are ignored."""
    firmware_controller._armed_deferred_installs.add("test_device.yaml")
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
    firmware_controller._armed_deferred_installs.add("test_device.yaml")
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
    firmware_controller._armed_deferred_installs.add("test_device.yaml")
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

    firmware_controller._db.devices.set_queued_update = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = True

    with patch(
        "esphome_device_builder.controllers.firmware.controller.runner.execute_job",
        new_callable=AsyncMock,
    ):
        await firmware_controller._execute_job(job, MagicMock())

    firmware_controller._db.devices.set_queued_update.assert_called_with(
        "test_device", is_queued=True
    )
    assert "test_device.yaml" in firmware_controller._armed_deferred_installs


@pytest.mark.asyncio
async def test_compile_only_does_not_arm_queue(firmware_controller, mock_device):
    """A plain compile job must NOT arm an auto-flash."""
    mock_device.state = DeviceState.OFFLINE
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"

    firmware_controller._db.devices.set_queued_update = MagicMock()

    # Create a job WITHOUT the is_deferred_install flag
    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = False

    with patch(
        "esphome_device_builder.controllers.firmware.controller.runner.execute_job",
        new_callable=AsyncMock,
    ):
        await firmware_controller._execute_job(job, MagicMock())

    # Ensure the arming path was skipped
    firmware_controller._db.devices.set_queued_update.assert_not_called()
    assert "test_device.yaml" not in firmware_controller._armed_deferred_installs


@pytest.mark.asyncio
async def test_execute_job_handles_online_device(firmware_controller, mock_device):
    """Test successful compile for an online device sets flag and triggers upload."""
    mock_device.state = DeviceState.ONLINE
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"
    firmware_controller._db.devices.set_queued_update = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = True

    with (
        patch(
            "esphome_device_builder.controllers.firmware.controller.runner.execute_job",
            new_callable=AsyncMock,
        ),
        patch.object(firmware_controller, "upload", new_callable=MagicMock) as mock_upload,
    ):
        await firmware_controller._execute_job(job, MagicMock())

    # Assert our new bugfix behavior: flag is persisted, but it is kept OUT of the armed set
    firmware_controller._db.devices.set_queued_update.assert_called_with(
        "test_device", is_queued=True
    )
    assert "test_device.yaml" not in firmware_controller._armed_deferred_installs
    mock_upload.assert_called_with(configuration="test_device.yaml", port="OTA")
    firmware_controller._db.create_background_task.assert_called_once()


def test_handle_device_wake_ignored_if_not_armed(firmware_controller, mock_device):
    """Test that an online event is ignored if the config is not in armed deferred installs."""
    mock_device.queued_update = True
    # Deliberately NOT adding 'test_device.yaml' to firmware_controller._armed_deferred_installs

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


@pytest.mark.asyncio
async def test_execute_job_clears_queued_flag_on_upload(firmware_controller, mock_device):
    """Test that a successful upload clears the queued update flag and arming set."""
    mock_device.queued_update = True
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"

    firmware_controller._db.devices.set_queued_update = MagicMock()
    firmware_controller._armed_deferred_installs.add("test_device.yaml")

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.UPLOAD
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = False
    job.port = "OTA"

    with patch(
        "esphome_device_builder.controllers.firmware.controller.runner.execute_job",
        new_callable=AsyncMock,
    ):
        await firmware_controller._execute_job(job, MagicMock())

    firmware_controller._db.devices.set_queued_update.assert_called_with(
        "test_device", is_queued=False
    )
    assert "test_device.yaml" not in firmware_controller._armed_deferred_installs


@pytest.mark.asyncio
async def test_execute_job_preserves_queued_flag_on_failed_upload(firmware_controller, mock_device):
    """Test that a failed OTA upload does NOT clear the queued flag and re-arms for retry."""
    mock_device.queued_update = True
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"

    firmware_controller._db.devices.set_queued_update = MagicMock()
    # Start disarmed — matches the state right after _handle_device_wake
    # dispatched this attempt (it discards before firing).
    firmware_controller._armed_deferred_installs.discard("test_device.yaml")

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.UPLOAD
    job.status = JobStatus.FAILED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = False
    job.port = "OTA"

    with patch(
        "esphome_device_builder.controllers.firmware.controller.runner.execute_job",
        new_callable=AsyncMock,
    ):
        await firmware_controller._execute_job(job, MagicMock())

    firmware_controller._db.devices.set_queued_update.assert_not_called()
    # Re-armed so the next wake retries.
    assert "test_device.yaml" in firmware_controller._armed_deferred_installs


def test_device_for_configuration_handles_none(firmware_controller):
    """Test helper bails safely if the devices controller is completely missing."""
    firmware_controller._db.devices = None
    assert firmware_controller._device_for_configuration("kitchen.yaml") is None


# --- _handle_deferred_compile_completion guard tests ---
def _make_deferred_compile_job(configuration: str = "test_device.yaml") -> MagicMock:
    """Build a completed deferred-install COMPILE job for guard-clause tests."""
    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.is_deferred_install = True
    job.configuration = configuration
    return job


def test_device_for_configuration_uses_get_by_configuration(firmware_controller):
    """Test standard production path using get_by_configuration()."""
    mock_device = MagicMock(configuration="kitchen.yaml")
    firmware_controller._db.devices = MagicMock()

    firmware_controller._db.devices.get_by_configuration.return_value = mock_device
    assert firmware_controller._device_for_configuration("kitchen.yaml") == mock_device


def test_device_for_configuration_handles_unknown_stub(firmware_controller):
    """Test the e2e StubDevices fallback that implements get_by_configuration()."""

    class StubDevices:
        def get_by_configuration(self, configuration: str):
            return None  # Real interface, empty result

    firmware_controller._db.devices = StubDevices()
    assert firmware_controller._device_for_configuration("kitchen.yaml") is None


def test_handle_deferred_compile_completion_no_op_when_devices_controller_is_none(
    firmware_controller,
):
    """Return early without arming when the devices controller is None."""

    # Setup: explicitly clear the devices controller
    firmware_controller._db.devices = None

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.is_deferred_install = True
    job.configuration = "some_device.yaml"

    # Execute: Should return safely without raising AttributeError
    firmware_controller._handle_deferred_compile_completion(job)

    # Assert: Nothing was armed
    assert "some_device.yaml" not in firmware_controller._armed_deferred_installs


def test_handle_deferred_compile_completion_no_op_when_device_not_found(
    firmware_controller,
):
    """Return early without arming when the configuration has no matching device."""
    # Setup: The O(1) shadow index lookup returns None (device not found)
    firmware_controller._db.devices.get_by_configuration.return_value = None
    firmware_controller._db.devices.set_queued_update = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.is_deferred_install = True
    job.configuration = "missing_device.yaml"

    firmware_controller._handle_deferred_compile_completion(job)

    # Assert we bailed out safely before trying to update or arm anything
    firmware_controller._db.devices.set_queued_update.assert_not_called()
    assert "missing_device.yaml" not in firmware_controller._armed_deferred_installs
