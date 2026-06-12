import pytest

from unittest.mock import AsyncMock, patch
from esphome_device_builder.models import DeviceState, JobType

@pytest.mark.asyncio
async def test_install_queues_for_offline_device(firmware_controller, mock_device):
    # Setup: Mock device as OFFLINE
    mock_device.state = DeviceState.OFFLINE

    # Execute install request
    with patch.object(firmware_controller, "_enqueue", new_callable=AsyncMock) as mock_enqueue:
        await firmware_controller.install(configuration="test_device.yaml")

        # Assert: It should NOT have called install_chain (which runs upload)
        # It should have called _enqueue with a COMPILE job
        called_job = mock_enqueue.call_args[0][0]
        assert called_job.job_type == JobType.COMPILE
        assert mock_device.queued_update is False # Flag set by runner completion