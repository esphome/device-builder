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
    controller._db.remote_build_offloader = None
    controller._db.create_background_task = MagicMock(side_effect=lambda coro: coro.close())
    controller.state = FirmwareState()
    controller._persist_jobs = AsyncMock()
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

    firmware_controller._db.devices.set_queued_update = MagicMock()

    await firmware_controller.clear_queued_update(configuration="test_device.yaml")

    firmware_controller._db.devices.clear_queued_update.assert_called_with("test_device.yaml")


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


def _devices_controller_with(mock_device):
    """Bare DevicesController wired for ``set_queued_update``."""
    controller = DevicesController.__new__(DevicesController)
    controller._scanner = MagicMock()
    controller._scanner.get_by_configuration.side_effect = lambda c: (
        mock_device if c == mock_device.configuration else None
    )
    controller._metadata_store = MagicMock()
    controller._fire_device_updated = MagicMock()
    return controller


def test_set_queued_update_persists_and_fires(mock_device):
    controller = _devices_controller_with(mock_device)

    assert controller.set_queued_update("test_device.yaml") is True

    assert mock_device.queued_update is True
    controller._metadata_store.update.assert_called_once_with(
        "test_device.yaml", queued_update=True
    )
    controller._fire_device_updated.assert_called_once_with(mock_device)


def test_set_queued_update_dedupes_same_value(mock_device):
    """A no-op flip neither persists nor fires."""
    mock_device.queued_update = True
    controller = _devices_controller_with(mock_device)

    assert controller.set_queued_update("test_device.yaml") is False

    controller._metadata_store.update.assert_not_called()
    controller._fire_device_updated.assert_not_called()


def test_set_queued_update_skips_unknown_configuration(mock_device):
    controller = _devices_controller_with(mock_device)

    assert controller.set_queued_update("other_device.yaml") is False

    assert mock_device.queued_update is False
    controller._metadata_store.update.assert_not_called()
    controller._fire_device_updated.assert_not_called()


# --- _handle_device tests ---
def test_handle_device_wake_triggers_upload(firmware_controller, mock_device):
    """Test that an online event for a device with a queued update triggers the upload."""
    mock_device.queued_update = True
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"

    firmware_controller._db.devices.set_queued_update = MagicMock()

    event = Event(
        EventType.DEVICE_STATE_CHANGED,
        data={
            "state": DeviceState.ONLINE.value,
            "configuration": "test_device.yaml",
        },
    )
    firmware_controller._handle_device_wake(event)

    firmware_controller._db.devices.set_queued_update.assert_not_called()
    uploads = [j for j in firmware_controller.state.jobs.values() if j.job_type is JobType.UPLOAD]
    assert len(uploads) == 1
    assert uploads[0].port == "OTA"
    firmware_controller._db.create_background_task.assert_called_once()


def test_handle_device_wake_ignored_if_offline(firmware_controller, mock_device):
    """Test that non-ONLINE state changes are ignored."""
    mock_device.queued_update = True
    event = Event(
        EventType.DEVICE_STATE_CHANGED,
        data={
            "state": DeviceState.OFFLINE.value,
            "configuration": "test_device.yaml",
        },
    )
    firmware_controller._handle_device_wake(event)
    assert not firmware_controller.state.jobs


def test_handle_device_wake_ignored_if_no_flag(firmware_controller, mock_device):
    """Test that online devices without the queued_update flag are ignored."""
    mock_device.queued_update = False
    event = Event(
        EventType.DEVICE_STATE_CHANGED,
        data={
            "state": DeviceState.ONLINE.value,
            "configuration": "test_device.yaml",
        },
    )
    firmware_controller._handle_device_wake(event)
    assert not firmware_controller.state.jobs


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

    firmware_controller._db.devices.set_queued_update = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.COMPILE
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = True
    job.is_deferred_compile_success = True
    job.is_terminal_ota_upload = False

    firmware_controller._handle_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    firmware_controller._db.devices.set_queued_update.assert_called_with("test_device.yaml")


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
    job.is_deferred_compile_success = False
    job.is_terminal_ota_upload = False

    firmware_controller._handle_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    # Ensure the arming path was skipped
    firmware_controller._db.devices.set_queued_update.assert_not_called()


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
    job.is_deferred_compile_success = True
    job.is_terminal_ota_upload = False

    firmware_controller._handle_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    firmware_controller._db.devices.set_queued_update.assert_called_with("test_device.yaml")
    uploads = [j for j in firmware_controller.state.jobs.values() if j.job_type is JobType.UPLOAD]
    assert [j.port for j in uploads] == ["OTA"]
    firmware_controller._db.create_background_task.assert_called_once()


def test_wake_flap_dispatches_a_single_upload(firmware_controller, mock_device):
    """A flap's second wake sees the synchronously-created job and backs off."""
    mock_device.queued_update = True
    event = Event(
        EventType.DEVICE_STATE_CHANGED,
        data={
            "state": DeviceState.ONLINE.value,
            "configuration": "test_device.yaml",
        },
    )

    firmware_controller._handle_device_wake(event)
    firmware_controller._handle_device_wake(event)

    uploads = [j for j in firmware_controller.state.jobs.values() if j.job_type is JobType.UPLOAD]
    assert len(uploads) == 1


def test_handle_device_wake_skips_active_flash(firmware_controller, mock_device):
    """A wake bouncing mid-flash must not supersede the upload already running."""
    mock_device.queued_update = True
    in_flight = FirmwareJob(
        job_id="u1",
        configuration="test_device.yaml",
        job_type=JobType.UPLOAD,
        status=JobStatus.RUNNING,
        port="OTA",
    )
    firmware_controller.state.jobs[in_flight.job_id] = in_flight

    event = Event(
        EventType.DEVICE_STATE_CHANGED,
        data={
            "state": DeviceState.ONLINE.value,
            "configuration": "test_device.yaml",
        },
    )
    firmware_controller._handle_device_wake(event)

    assert list(firmware_controller.state.jobs) == ["u1"]


def test_handle_device_wake_triggers_after_rename(firmware_controller, mock_device):
    """The arm is the device's persisted flag, so it survives a rename's new filename."""
    mock_device.queued_update = True
    mock_device.configuration = "renamed_device.yaml"
    mock_device.name = "renamed_device"

    event = Event(
        EventType.DEVICE_STATE_CHANGED,
        data={
            "state": DeviceState.ONLINE.value,
            "configuration": "renamed_device.yaml",
        },
    )
    firmware_controller._handle_device_wake(event)

    uploads = [j for j in firmware_controller.state.jobs.values() if j.job_type is JobType.UPLOAD]
    assert [j.configuration for j in uploads] == ["renamed_device.yaml"]


@pytest.mark.asyncio
async def test_execute_job_clears_queued_flag_on_upload(firmware_controller, mock_device):
    """Test that a successful upload clears the queued update flag and arming set."""
    mock_device.queued_update = True
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"

    firmware_controller._db.devices.set_queued_update = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.UPLOAD
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = False
    job.port = "OTA"
    job.is_deferred_compile_success = False
    job.is_terminal_ota_upload = True

    firmware_controller._handle_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    firmware_controller._db.devices.clear_queued_update.assert_called_with("test_device.yaml")


@pytest.mark.asyncio
async def test_execute_job_preserves_queued_flag_on_failed_upload(firmware_controller, mock_device):
    """Test that a failed OTA upload does NOT clear the queued flag and re-arms for retry."""
    mock_device.queued_update = True
    mock_device.configuration = "test_device.yaml"
    mock_device.name = "test_device"

    firmware_controller._db.devices.set_queued_update = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.UPLOAD
    job.status = JobStatus.FAILED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = False
    job.port = "OTA"
    job.is_deferred_compile_success = False
    job.is_terminal_ota_upload = True

    # Failed uploads never reach the JOB_COMPLETED listener; the handler's
    # own terminal guard keeps a direct call a no-op too.
    firmware_controller._handle_ota_upload_completion(job)

    # The flag stays set, so the device stays armed for its next wake.
    firmware_controller._db.devices.clear_queued_update.assert_not_called()


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
    job.is_deferred_compile_success = True
    job.is_terminal_ota_upload = False
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

    job = _make_deferred_compile_job("some_device.yaml")

    # Should return safely without raising AttributeError
    firmware_controller._handle_deferred_compile_completion(job)


def test_handle_deferred_compile_completion_no_op_when_device_not_found(
    firmware_controller,
):
    """Return early without arming when the configuration has no matching device."""
    # Setup: The O(1) shadow index lookup returns None (device not found)
    firmware_controller._db.devices.get_by_configuration.return_value = None
    firmware_controller._db.devices.set_queued_update = MagicMock()

    job = _make_deferred_compile_job("missing_device.yaml")

    firmware_controller._handle_deferred_compile_completion(job)

    # Bailed out safely before trying to update or arm anything
    firmware_controller._db.devices.set_queued_update.assert_not_called()


@pytest.mark.asyncio
async def test_clean_disarms_queued_update(firmware_controller, mock_device):
    """A wiped build tree can't flash; clean must clear the arm."""
    mock_device.queued_update = True
    firmware_controller._db.devices.set_queued_update = MagicMock()

    with patch(
        "esphome_device_builder.controllers.firmware.controller.clean_mod.clean",
        new_callable=AsyncMock,
    ):
        await firmware_controller.clean(configuration="test_device.yaml")

    firmware_controller._db.devices.clear_queued_update.assert_called_with("test_device.yaml")


@pytest.mark.asyncio
async def test_reset_build_env_disarms_all_queued_updates(firmware_controller, mock_device):
    """The global wipe clears every device's arm, not just one config's."""
    mock_device.queued_update = True
    other = MagicMock(configuration="other.yaml", queued_update=True)
    unarmed = MagicMock(configuration="idle.yaml", queued_update=False)
    firmware_controller._db.devices.get_devices.return_value = [mock_device, other, unarmed]
    firmware_controller._db.devices.set_queued_update = MagicMock()

    with (
        patch(
            "esphome_device_builder.controllers.firmware.controller.factories.cancel_all_active_jobs",
            new_callable=AsyncMock,
        ),
        patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock),
    ):
        await firmware_controller.reset_build_env()

    calls = firmware_controller._db.devices.clear_queued_update.call_args_list
    cleared = {c.args[0] for c in calls}
    assert cleared == {"test_device.yaml", "other.yaml"}


def test_completed_ota_upload_for_unarmed_device_is_ignored(firmware_controller, mock_device):
    """A regular install's OTA upload must not touch the queue machinery."""
    mock_device.queued_update = False
    firmware_controller._db.devices.clear_queued_update = MagicMock()

    job = MagicMock(spec=FirmwareJob)
    job.job_type = JobType.UPLOAD
    job.status = JobStatus.COMPLETED
    job.configuration = "test_device.yaml"
    job.is_deferred_install = False
    job.port = "OTA"
    job.is_deferred_compile_success = False
    job.is_terminal_ota_upload = True

    firmware_controller._handle_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    firmware_controller._db.devices.clear_queued_update.assert_not_called()
