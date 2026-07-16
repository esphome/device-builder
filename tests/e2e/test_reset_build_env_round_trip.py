"""End-to-end: ``reset_build_env`` round-trip across the live peer-link.

Drives the real Noise channel + receive loops: the offloader's
:meth:`PeerLinkClient.reset_build_env` frame lands in the receiver's
``_dispatch_reset_build_env``, which enqueues a tagged RESET_BUILD_ENV
job and acks — or refuses ``busy`` without touching the queue.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from esphome_device_builder.models import FirmwareJob, JobStatus, JobType

from .conftest import PairedInstances


def _wire_receiver_firmware(
    instances: PairedInstances, *, active_jobs: list[Any] | None = None
) -> list[FirmwareJob]:
    """Give the receiver's firmware stub the surface the reset handler touches."""
    created_jobs: list[FirmwareJob] = []

    def _create_job(
        configuration: str,
        job_type: JobType,
        *,
        remote_peer: str = "",
        remote_peer_label: str = "",
        remote_job_id: str = "",
        **_: Any,
    ) -> FirmwareJob:
        job = FirmwareJob(
            job_id=f"rcv-{len(created_jobs)}",
            configuration=configuration,
            job_type=job_type,
            status=JobStatus.QUEUED,
            remote_peer=remote_peer,
            remote_peer_label=remote_peer_label,
            remote_job_id=remote_job_id,
        )
        created_jobs.append(job)
        return job

    firmware = instances.receiver._db.firmware
    firmware._create_job = MagicMock(side_effect=_create_job)
    firmware._enqueue = AsyncMock(side_effect=lambda job, **_: job)
    jobs = active_jobs if active_jobs is not None else []
    firmware.state.active_jobs = MagicMock(side_effect=lambda: iter(jobs))
    firmware.state.jobs = {}
    return created_jobs


async def test_reset_build_env_round_trip_enqueues_tagged_job(
    paired_instances: PairedInstances,
) -> None:
    """The reset frame lands a tagged RESET_BUILD_ENV job and the ack rides back."""
    await paired_instances.wait_until_session_opened()
    created_jobs = _wire_receiver_firmware(paired_instances)

    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.reset_build_env(job_id="off-reset-1")

    assert ack["accepted"] is True
    assert ack["job_id"] == "off-reset-1"
    assert "reason" not in ack

    assert len(created_jobs) == 1
    job = created_jobs[0]
    assert job.job_type is JobType.RESET_BUILD_ENV
    assert job.configuration == ""
    assert job.remote_peer == paired_instances.offloader_dashboard_id
    assert job.remote_job_id == "off-reset-1"
    firmware = paired_instances.receiver._db.firmware
    firmware._disarm_all_queued_updates.assert_called_once()
    firmware._enqueue.assert_awaited_once_with(job, supersede=False)


async def test_reset_build_env_round_trip_refuses_busy(
    paired_instances: PairedInstances,
) -> None:
    """An active job on the receiver refuses the reset without touching the queue."""
    await paired_instances.wait_until_session_opened()
    _wire_receiver_firmware(paired_instances, active_jobs=[MagicMock()])

    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.reset_build_env(job_id="off-reset-2")

    assert ack["accepted"] is False
    assert ack["reason"] == "busy"
    paired_instances.receiver._db.firmware._create_job.assert_not_called()
