"""Firmware-job factories: create, source-resolve, rename-lock, enqueue, supersede."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from ...helpers.api import CommandError
from ...helpers.build_scheduler import BuildPath, pick_build_path
from ...models import (
    LOCAL_JOB_BUILD_SOURCE,
    ErrorCode,
    EventType,
    FirmwareJob,
    JobBuildSource,
    JobLifecycleData,
    JobSource,
    JobType,
)
from .constants import _ACTIVE_JOB_STATUSES
from .helpers import _names_touched_by_job

if TYPE_CHECKING:
    from .controller import FirmwareController


def create_job(
    controller: FirmwareController,
    configuration: str,
    job_type: JobType,
    port: str = "",
    new_name: str = "",
    remote_peer: str = "",
    remote_peer_label: str = "",
    remote_job_id: str = "",
    build_source: JobBuildSource = LOCAL_JOB_BUILD_SOURCE,
    device_name: str = "",
    device_friendly_name: str = "",
) -> FirmwareJob:
    """Create a new job and add it to the in-memory map; *sync*, no I/O.

    Caller validates ``configuration`` via
    ``_validate_configuration_boundary`` first. The ``remote_*``
    fields identify receiver-side jobs from peer-link ``submit_job``
    — empty for local-origin jobs.
    """
    job = FirmwareJob(
        job_id=uuid4().hex[:12],
        configuration=configuration,
        job_type=job_type,
        created_at=datetime.now(UTC).isoformat(),
        port=port,
        new_name=new_name,
        remote_peer=remote_peer,
        remote_peer_label=remote_peer_label,
        remote_job_id=remote_job_id,
        source=build_source.source,
        source_pin_sha256=build_source.source_pin_sha256,
        source_label=build_source.source_label,
        source_esphome_version=build_source.source_esphome_version,
        device_name=device_name,
        device_friendly_name=device_friendly_name,
    )
    controller._jobs[job.job_id] = job
    return job


def resolve_install_source(
    controller: FirmwareController, *, force_local: bool = False
) -> JobBuildSource:
    """Pick LOCAL or REMOTE from the scheduler snapshot; return the build source."""
    if force_local:
        return LOCAL_JOB_BUILD_SOURCE
    offloader = controller._db.remote_build_offloader
    if offloader is None:
        return LOCAL_JOB_BUILD_SOURCE
    decision = pick_build_path(offloader.build_scheduler_snapshot())
    if decision.path is not BuildPath.REMOTE or decision.pin_sha256 is None:
        return LOCAL_JOB_BUILD_SOURCE
    pairing = offloader.get_pairing(decision.pin_sha256)
    if pairing is None:
        # Scheduler picked a pin that's no longer paired (race vs
        # an ``unpair`` on the same loop tick).
        return LOCAL_JOB_BUILD_SOURCE
    return JobBuildSource(
        source=JobSource.REMOTE,
        source_pin_sha256=pairing.pin_sha256,
        source_label=pairing.label,
        source_esphome_version=pairing.esphome_version,
    )


async def enqueue(
    controller: FirmwareController, job: FirmwareJob, *, supersede: bool = True
) -> FirmwareJob:
    """Enqueue *job*, persist, fire JOB_QUEUED; cancel predecessors by default.

    Fires JOB_QUEUED *before* cancelling any predecessor for the
    same configuration so frontends recognise the resulting
    JOB_CANCELLED as a supersede and drop the old entry silently.
    Reset jobs (empty configuration) skip the supersede.

    ``supersede=False`` opts out — used by the ``firmware/clean``
    fan-out so per-peer remote-fan-out jobs don't cancel their
    siblings or the just-queued local job (#608).

    Rejects with ``CommandError(INVALID_ARGS)`` when an in-flight
    RENAME has *job*'s configuration locked.
    """
    controller._check_rename_lock(job)
    await controller._queue.put(job)
    queued_payload: JobLifecycleData = {"job": job}
    controller._db.bus.fire(EventType.JOB_QUEUED, queued_payload)
    if supersede and job.configuration:
        await controller._supersede_active_jobs(job.configuration, exclude_job_id=job.job_id)
    await controller._persist_jobs()
    return job


def check_rename_lock(controller: FirmwareController, job: FirmwareJob) -> None:
    """Reject *job* if an in-flight rename has either YAML name locked.

    A rename touches two filenames (the old it reads from + the
    new it creates on install success); conflicting jobs would
    fight for the same file or land work on a half-flashed device.
    Same-old-config ``RENAME`` retries pass through so supersede
    can cancel-and-replace.
    """
    new_touches = _names_touched_by_job(job)
    if not new_touches:
        return
    for active in controller._jobs.values():
        if active.job_type != JobType.RENAME:
            continue
        if active.status not in _ACTIVE_JOB_STATUSES:
            continue
        # Same-old-config rename retry: let supersede do its thing.
        if job.job_type == JobType.RENAME and job.configuration == active.configuration:
            continue
        clash = new_touches & _names_touched_by_job(active)
        if not clash:
            continue
        old = active.configuration
        new = f"{active.new_name}.yaml" if active.new_name else "(unknown)"
        msg = (
            f"Device {old} is being renamed to {new}; wait for the "
            f"rename to finish before queueing another firmware "
            f"task on either name."
        )
        raise CommandError(ErrorCode.INVALID_ARGS, msg)


async def supersede_active_jobs(
    controller: FirmwareController, configuration: str, *, exclude_job_id: str
) -> None:
    """Cancel queued/running jobs for ``configuration``."""
    to_cancel = [
        j.job_id
        for j in controller._jobs.values()
        if j.job_id != exclude_job_id
        and j.configuration == configuration
        and j.status in _ACTIVE_JOB_STATUSES
    ]
    for job_id in to_cancel:
        # Status may flip under us if the runner finalises the
        # job mid-iteration; cancel() raises in that window and
        # we don't care.
        with suppress(ValueError, RuntimeError):
            await controller.cancel(job_id=job_id)
