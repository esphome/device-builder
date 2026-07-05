"""Tests for the queued offline updates feature."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from esphome_device_builder.controllers.devices.controller import DevicesController
from esphome_device_builder.controllers.firmware._state import FirmwareState
from esphome_device_builder.controllers.firmware.controller import FirmwareController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.event_bus import Event
from esphome_device_builder.models import (
    DeviceState,
    ErrorCode,
    EventType,
    FirmwareJob,
    JobStatus,
    JobType,
)


def _job(
    job_type: JobType,
    status: JobStatus,
    *,
    configuration: str = "test_device.yaml",
    port: str = "",
    deferred: bool = False,
) -> FirmwareJob:
    """Real ``FirmwareJob`` so the handlers exercise the real property logic."""
    job = FirmwareJob(
        job_id="j1", configuration=configuration, job_type=job_type, status=status, port=port
    )
    job.is_deferred_install = deferred
    return job


def _wake_event(configuration: str, state: DeviceState = DeviceState.ONLINE) -> Event:
    return Event(
        EventType.DEVICE_STATE_CHANGED,
        data={"state": state.value, "configuration": configuration},
    )


def _completed(job: FirmwareJob) -> Event:
    return Event(EventType.JOB_COMPLETED, {"job": job})


@pytest.fixture
def mock_device() -> MagicMock:
    """Mock device for offline update tests."""
    mock = MagicMock()
    mock.state = DeviceState.OFFLINE
    mock.queued_update = False
    mock.name = "test_device"
    mock.configuration = "test_device.yaml"
    return mock


@pytest.fixture
def firmware_controller(mock_device: MagicMock) -> FirmwareController:
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
async def test_install_queues_deferred_compile_for_offline_device(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """Test that offline devices queue a COMPILE job marked as a deferred install."""
    mock_device.state = DeviceState.OFFLINE

    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock) as mock_enqueue:
        await firmware_controller.install(configuration="test_device.yaml")

        called_job = mock_enqueue.call_args[0][0]
        assert called_job.job_type == JobType.COMPILE
        assert called_job.is_deferred_install is True


@pytest.mark.asyncio
async def test_install_bootloader_refuses_offline_device(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A bootloader install never defers — the wake dispatch flashes the app, not the bootloader."""
    mock_device.state = DeviceState.OFFLINE

    with pytest.raises(CommandError) as err:
        await firmware_controller.install(configuration="test_device.yaml", bootloader=True)

    assert err.value.code == ErrorCode.INVALID_ARGS
    assert not firmware_controller.state.jobs


@pytest.mark.asyncio
async def test_install_bootloader_refuses_offline_device_with_explicit_target(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """An explicit IP target doesn't bypass the OFFLINE bootloader rejection."""
    mock_device.state = DeviceState.OFFLINE

    with pytest.raises(CommandError) as err:
        await firmware_controller.install(
            configuration="test_device.yaml", port="192.168.1.5", bootloader=True
        )

    assert err.value.code == ErrorCode.INVALID_ARGS
    assert not firmware_controller.state.jobs


@pytest.mark.asyncio
async def test_compile_does_not_mark_deferred(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """Test that a plain compile does NOT mark the job as a deferred install."""
    mock_device.state = DeviceState.OFFLINE

    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock) as mock_enqueue:
        await firmware_controller.compile(configuration="test_device.yaml")

        called_job = mock_enqueue.call_args[0][0]
        assert called_job.job_type == JobType.COMPILE
        assert called_job.is_deferred_install is False


@pytest.mark.asyncio
async def test_clear_queued_update_clears_flag(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """Test that clear_queued_update command resets the queued_update flag."""
    mock_device.state = DeviceState.OFFLINE
    mock_device.queued_update = True

    await firmware_controller.clear_queued_update(configuration="test_device.yaml")

    firmware_controller._db.devices.clear_queued_update.assert_called_with("test_device.yaml")


@pytest.mark.asyncio
async def test_clear_queued_update_invalid_config_raises(
    firmware_controller: FirmwareController,
) -> None:
    """Test that clearing an invalid device configuration raises an error."""
    firmware_controller._db.settings.rel_path.side_effect = ValueError("Out of bounds")

    # Assert that exactly a ValueError is raised with our specific message
    with pytest.raises(ValueError, match="Out of bounds"):
        await firmware_controller.clear_queued_update(configuration="non_existent.yaml")


@pytest.mark.asyncio
async def test_queued_update_not_cleared_if_device_missing(
    firmware_controller: FirmwareController,
) -> None:
    """Test that the command handles missing device objects gracefully."""
    # Setup: Force _db.devices to None
    firmware_controller._db.devices = None

    # Should not raise exception, just return None
    result = await firmware_controller.clear_queued_update(configuration="test_device.yaml")
    assert result is None


# --- DevicesController.set_queued_update / clear_queued_update ---
def _devices_controller_with(mock_device: MagicMock) -> DevicesController:
    """Bare DevicesController wired for ``set_queued_update``."""
    controller = DevicesController.__new__(DevicesController)
    controller._scanner = MagicMock()
    controller._scanner.get_by_configuration.side_effect = lambda c: (
        mock_device if c == mock_device.configuration else None
    )
    controller._metadata_store = MagicMock()
    controller._fire_device_updated = MagicMock()
    return controller


def test_set_queued_update_persists_and_fires(mock_device: MagicMock) -> None:
    controller = _devices_controller_with(mock_device)

    assert controller.set_queued_update("test_device.yaml") is True

    assert mock_device.queued_update is True
    controller._metadata_store.update.assert_called_once_with(
        "test_device.yaml", queued_update=True
    )
    controller._fire_device_updated.assert_called_once_with(mock_device)


def test_clear_queued_update_persists_and_fires(mock_device: MagicMock) -> None:
    mock_device.queued_update = True
    controller = _devices_controller_with(mock_device)

    assert controller.clear_queued_update("test_device.yaml") is True

    assert mock_device.queued_update is False
    controller._metadata_store.update.assert_called_once_with(
        "test_device.yaml", queued_update=False
    )
    controller._fire_device_updated.assert_called_once_with(mock_device)


def test_set_queued_update_dedupes_same_value(mock_device: MagicMock) -> None:
    """A no-op flip neither persists nor fires."""
    mock_device.queued_update = True
    controller = _devices_controller_with(mock_device)

    assert controller.set_queued_update("test_device.yaml") is False

    controller._metadata_store.update.assert_not_called()
    controller._fire_device_updated.assert_not_called()


def test_set_queued_update_skips_unknown_configuration(mock_device: MagicMock) -> None:
    controller = _devices_controller_with(mock_device)

    assert controller.set_queued_update("other_device.yaml") is False

    assert mock_device.queued_update is False
    controller._metadata_store.update.assert_not_called()
    controller._fire_device_updated.assert_not_called()


# --- _handle_device_wake ---
def test_handle_device_wake_triggers_upload(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """Test that an online event for a device with a queued update triggers the upload."""
    mock_device.queued_update = True

    firmware_controller._handle_device_wake(_wake_event("test_device.yaml"))

    firmware_controller._db.devices.set_queued_update.assert_not_called()
    uploads = [j for j in firmware_controller.state.jobs.values() if j.job_type is JobType.UPLOAD]
    assert len(uploads) == 1
    assert uploads[0].port == "OTA"
    firmware_controller._db.create_background_task.assert_called_once()


def test_handle_device_wake_ignored_if_offline(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """Test that non-ONLINE state changes are ignored."""
    mock_device.queued_update = True

    firmware_controller._handle_device_wake(
        _wake_event("test_device.yaml", state=DeviceState.OFFLINE)
    )

    assert not firmware_controller.state.jobs


def test_handle_device_wake_ignored_if_no_flag(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """Test that online devices without the queued_update flag are ignored."""
    mock_device.queued_update = False

    firmware_controller._handle_device_wake(_wake_event("test_device.yaml"))

    assert not firmware_controller.state.jobs


def test_handle_device_wake_no_devices(firmware_controller: FirmwareController) -> None:
    """Test that the handler safely bails if the devices controller is None."""
    firmware_controller._db.devices = None

    # Should not raise
    firmware_controller._handle_device_wake(_wake_event("test_device.yaml"))


def test_wake_flap_dispatches_a_single_upload(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A flap's second wake sees the synchronously-created job and backs off."""
    mock_device.queued_update = True
    event = _wake_event("test_device.yaml")

    firmware_controller._handle_device_wake(event)
    firmware_controller._handle_device_wake(event)

    uploads = [j for j in firmware_controller.state.jobs.values() if j.job_type is JobType.UPLOAD]
    assert len(uploads) == 1


def test_handle_device_wake_skips_active_flash(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A wake bouncing mid-flash must not supersede the upload already running."""
    mock_device.queued_update = True
    in_flight = _job(JobType.UPLOAD, JobStatus.RUNNING, port="OTA")
    firmware_controller.state.jobs[in_flight.job_id] = in_flight

    firmware_controller._handle_device_wake(_wake_event("test_device.yaml"))

    assert list(firmware_controller.state.jobs) == ["j1"]


def test_handle_device_wake_triggers_after_rename(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """The arm is the device's persisted flag, so it survives a rename's new filename."""
    mock_device.queued_update = True
    mock_device.configuration = "renamed_device.yaml"
    mock_device.name = "renamed_device"

    firmware_controller._handle_device_wake(_wake_event("renamed_device.yaml"))

    uploads = [j for j in firmware_controller.state.jobs.values() if j.job_type is JobType.UPLOAD]
    assert [j.configuration for j in uploads] == ["renamed_device.yaml"]


# --- JOB_COMPLETED listener: arm / dispatch / disarm ---
def test_completed_deferred_compile_arms_offline_device(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A finished deferred compile for a still-offline device arms it for wake."""
    mock_device.state = DeviceState.OFFLINE

    firmware_controller._handle_job_completed(
        _completed(_job(JobType.COMPILE, JobStatus.COMPLETED, deferred=True))
    )

    firmware_controller._db.devices.set_queued_update.assert_called_with("test_device.yaml")


def test_completed_plain_compile_does_not_arm(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A plain compile job must NOT arm an auto-flash."""
    mock_device.state = DeviceState.OFFLINE

    firmware_controller._handle_job_completed(
        _completed(_job(JobType.COMPILE, JobStatus.COMPLETED))
    )

    firmware_controller._db.devices.set_queued_update.assert_not_called()


def test_completed_deferred_compile_flashes_online_device_now(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A finished deferred compile for an already-online device arms and flashes."""
    mock_device.state = DeviceState.ONLINE

    firmware_controller._handle_job_completed(
        _completed(_job(JobType.COMPILE, JobStatus.COMPLETED, deferred=True))
    )

    firmware_controller._db.devices.set_queued_update.assert_called_with("test_device.yaml")
    uploads = [j for j in firmware_controller.state.jobs.values() if j.job_type is JobType.UPLOAD]
    assert [j.port for j in uploads] == ["OTA"]
    firmware_controller._db.create_background_task.assert_called_once()


def test_completed_ota_upload_disarms(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A delivered OTA upload clears the queued update flag."""
    mock_device.queued_update = True

    firmware_controller._handle_job_completed(
        _completed(_job(JobType.UPLOAD, JobStatus.COMPLETED, port="OTA"))
    )

    firmware_controller._db.devices.clear_queued_update.assert_called_with("test_device.yaml")


def test_failed_ota_upload_keeps_the_device_armed(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A failed OTA upload leaves the flag set so the next wake retries."""
    mock_device.queued_update = True

    firmware_controller._handle_ota_upload_completion(
        _job(JobType.UPLOAD, JobStatus.FAILED, port="OTA")
    )

    firmware_controller._db.devices.clear_queued_update.assert_not_called()


def test_completed_ota_upload_for_unarmed_device_is_ignored(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A regular install's OTA upload must not touch the queue machinery."""
    mock_device.queued_update = False

    firmware_controller._handle_job_completed(
        _completed(_job(JobType.UPLOAD, JobStatus.COMPLETED, port="OTA"))
    )

    firmware_controller._db.devices.clear_queued_update.assert_not_called()


# --- guards + helpers ---
def test_device_for_configuration_handles_none(firmware_controller: FirmwareController) -> None:
    """Test helper bails safely if the devices controller is completely missing."""
    firmware_controller._db.devices = None
    assert firmware_controller._device_for_configuration("kitchen.yaml") is None


def test_device_for_configuration_uses_get_by_configuration(
    firmware_controller: FirmwareController,
) -> None:
    """Test standard production path using get_by_configuration()."""
    mock_device = MagicMock(configuration="kitchen.yaml")
    firmware_controller._db.devices = MagicMock()

    firmware_controller._db.devices.get_by_configuration.return_value = mock_device
    assert firmware_controller._device_for_configuration("kitchen.yaml") == mock_device


def test_device_for_configuration_handles_unknown_stub(
    firmware_controller: FirmwareController,
) -> None:
    """Test the e2e StubDevices fallback that implements get_by_configuration()."""

    class StubDevices:
        def get_by_configuration(self, configuration: str):
            return None  # Real interface, empty result

    firmware_controller._db.devices = StubDevices()
    assert firmware_controller._device_for_configuration("kitchen.yaml") is None


def test_handle_deferred_compile_completion_no_op_when_devices_controller_is_none(
    firmware_controller: FirmwareController,
) -> None:
    """Return early without arming when the devices controller is None."""
    firmware_controller._db.devices = None

    # Should return safely without raising AttributeError
    firmware_controller._handle_deferred_compile_completion(
        _job(JobType.COMPILE, JobStatus.COMPLETED, configuration="some_device.yaml", deferred=True)
    )


def test_handle_deferred_compile_completion_no_op_when_device_not_found(
    firmware_controller: FirmwareController,
) -> None:
    """Return early without arming when the configuration has no matching device."""
    firmware_controller._db.devices.get_by_configuration.side_effect = None
    firmware_controller._db.devices.get_by_configuration.return_value = None

    firmware_controller._handle_deferred_compile_completion(
        _job(
            JobType.COMPILE, JobStatus.COMPLETED, configuration="missing_device.yaml", deferred=True
        )
    )

    # Bailed out safely before trying to update or arm anything
    firmware_controller._db.devices.set_queued_update.assert_not_called()


@pytest.mark.asyncio
async def test_clean_disarms_queued_update(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """A wiped build tree can't flash; clean must clear the arm."""
    mock_device.queued_update = True

    with patch(
        "esphome_device_builder.controllers.firmware.controller.clean_mod.clean",
        new_callable=AsyncMock,
    ):
        await firmware_controller.clean(configuration="test_device.yaml")

    firmware_controller._db.devices.clear_queued_update.assert_called_with("test_device.yaml")


@pytest.mark.asyncio
async def test_reset_build_env_disarms_all_queued_updates(
    firmware_controller: FirmwareController,
    mock_device: MagicMock,
) -> None:
    """The global wipe clears every device's arm, not just one config's."""
    mock_device.queued_update = True
    other = MagicMock(configuration="other.yaml", queued_update=True)
    unarmed = MagicMock(configuration="idle.yaml", queued_update=False)
    firmware_controller._db.devices.get_devices.return_value = [mock_device, other, unarmed]

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


# --- FirmwareJob property contracts ---
def test_is_completed_ota_upload_truth_table() -> None:
    assert _job(JobType.UPLOAD, JobStatus.COMPLETED, port="OTA").is_completed_ota_upload is True
    assert _job(JobType.UPLOAD, JobStatus.FAILED, port="OTA").is_completed_ota_upload is False
    assert _job(JobType.UPLOAD, JobStatus.RUNNING, port="OTA").is_completed_ota_upload is False
    assert (
        _job(JobType.UPLOAD, JobStatus.COMPLETED, port="/dev/ttyUSB0").is_completed_ota_upload
        is False
    )
    assert _job(JobType.COMPILE, JobStatus.COMPLETED, port="OTA").is_completed_ota_upload is False


def test_is_deferred_compile_success_truth_table() -> None:
    assert (
        _job(JobType.COMPILE, JobStatus.COMPLETED, deferred=True).is_deferred_compile_success
        is True
    )
    assert (
        _job(JobType.COMPILE, JobStatus.COMPLETED, deferred=False).is_deferred_compile_success
        is False
    )
    assert (
        _job(JobType.COMPILE, JobStatus.FAILED, deferred=True).is_deferred_compile_success is False
    )
    assert (
        _job(JobType.UPLOAD, JobStatus.COMPLETED, deferred=True).is_deferred_compile_success
        is False
    )
