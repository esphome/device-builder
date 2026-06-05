"""Firmware-job factories: create, source-resolve, rename-lock, enqueue, supersede."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from ...helpers.api import CommandError
from ...helpers.build_scheduler import BuildPath, pick_build_path
from ...models import (
    LOCAL_JOB_BUILD_SOURCE,
    REMOTE_PENDING_JOB_BUILD_SOURCE,
    ErrorCode,
    EventType,
    FirmwareJob,
    JobBuildSource,
    JobType,
)
from ...models.firmware import _now_iso
from .helpers import _fire_job_lifecycle, _names_touched_by_job

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
    depends_on: str = "",
) -> FirmwareJob:
    """Create a new job and add it to the in-memory map; *sync*, no I/O.

    Caller validates ``configuration`` via
    ``_validate_configuration_boundary`` first. The ``remote_*``
    fields identify receiver-side jobs from peer-link ``submit_job``
    — empty for local-origin jobs. ``depends_on`` chains a job behind
    a prerequisite (the UPLOAD half of an install).
    """
    job = FirmwareJob(
        job_id=uuid4().hex[:12],
        configuration=configuration,
        job_type=job_type,
        created_at=_now_iso(),
        port=port,
        new_name=new_name,
        depends_on=depends_on,
        remote_peer=remote_peer,
        remote_peer_label=remote_peer_label,
        remote_job_id=remote_job_id,
        device_name=device_name,
        device_friendly_name=device_friendly_name,
    )
    job.apply_build_source(build_source)
    controller.state.jobs[job.job_id] = job
    return job


def resolve_install_source(
    controller: FirmwareController, *, force_local: bool = False
) -> JobBuildSource:
    """Decide LOCAL vs deferred-REMOTE for a new compile; the pin binds at dispatch.

    Returns ``REMOTE_PENDING`` when a paired server is eligible — the
    remote-dispatch pool picks *which* server when one frees, so a host
    paired/freed mid-queue is used. ``EXACT_REQUIRED`` with no compatible
    peer still raises ``NO_COMPATIBLE_PEER`` here for a synchronous toast.
    """
    if force_local:
        return LOCAL_JOB_BUILD_SOURCE
    offloader = controller._db.remote_build_offloader
    if offloader is None:
        return LOCAL_JOB_BUILD_SOURCE
    decision = pick_build_path(offloader.build_scheduler_snapshot())
    if decision.path is BuildPath.REMOTE:
        return REMOTE_PENDING_JOB_BUILD_SOURCE
    return LOCAL_JOB_BUILD_SOURCE


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
    _place_and_announce(controller, job)
    if supersede and job.configuration:
        await controller._supersede_active_jobs(job.configuration, exclude_job_ids={job.job_id})
    await controller._persist_jobs()
    return job


def _place_and_announce(controller: FirmwareController, job: FirmwareJob) -> None:
    """Put *job* on its lane (when its prerequisite is met) and fire JOB_QUEUED.

    A job with an unmet ``depends_on`` is held off its lane —
    ``lifecycle.release_dependents`` lands it when the prerequisite finishes —
    but still fires JOB_QUEUED so it renders as queued. Synchronous so a caller
    can commit several jobs atomically before its first ``await``.
    """
    if controller.state.dependency_satisfied(job):
        controller.state.place_on_lane(job)
    _fire_job_lifecycle(job, controller._db.bus, EventType.JOB_QUEUED)


async def enqueue_install_chain(
    controller: FirmwareController,
    *,
    configuration: str,
    port: str,
    build_source: JobBuildSource,
) -> FirmwareJob:
    """Enqueue an install as a COMPILE job + a dependent local UPLOAD job.

    Returns the COMPILE job (the chain head). The UPLOAD is held until the
    compile succeeds, then runs on the upload lane — so the network flash
    doesn't block the next device's compile. Both are created before either
    enqueues so a fast compile can't finish before the dependent exists; the
    upload enqueues *held* first so the compile completing can't double-add
    it.
    """
    compile_job = create_job(controller, configuration, JobType.COMPILE, build_source=build_source)
    upload_job = create_job(
        controller, configuration, JobType.UPLOAD, port=port, depends_on=compile_job.job_id
    )
    # Commit the pair atomically: one rename-lock check, then place + announce
    # both synchronously (no await between), one supersede + one persist below.
    # That closes the window the two-await shape left, where a rename acquired
    # during the first enqueue's persist-await stranded a half-queued pair on
    # disk. Both jobs share a configuration, so one check covers both. The
    # upload (unmet prerequisite) is held off its lane until the compile lands
    # it via ``release_dependents``.
    try:
        controller._check_rename_lock(compile_job)
    except CommandError:
        controller.state.jobs.pop(upload_job.job_id, None)
        controller.state.jobs.pop(compile_job.job_id, None)
        raise
    _place_and_announce(controller, upload_job)
    _place_and_announce(controller, compile_job)
    await controller._supersede_active_jobs(
        configuration, exclude_job_ids={compile_job.job_id, upload_job.job_id}
    )
    await controller._persist_jobs()
    return compile_job


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
    for active in controller.state.active_jobs():
        if active.job_type != JobType.RENAME:
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
    controller: FirmwareController, configuration: str, *, exclude_job_ids: set[str]
) -> None:
    """Cancel queued/running jobs for ``configuration``, except *exclude_job_ids*.

    Takes a set so an install chain (COMPILE + dependent UPLOAD) can exclude
    both halves and not cancel its own sibling.
    """
    await _cancel_active_jobs(
        controller,
        exclude_job_ids=exclude_job_ids,
        configuration=configuration,
    )


async def cancel_all_active_jobs(
    controller: FirmwareController, *, exclude_job_ids: set[str]
) -> None:
    """Cancel every queued/running job on either lane, except *exclude_job_ids*.

    For ``reset_build_env`` (clean-all), which wipes the whole build tree.
    """
    await _cancel_active_jobs(controller, exclude_job_ids=exclude_job_ids)


async def _cancel_active_jobs(
    controller: FirmwareController,
    *,
    exclude_job_ids: set[str],
    configuration: str | None = None,
) -> None:
    """Cancel active jobs (optionally scoped to *configuration*), swallowing benign races.

    A ``RuntimeError`` means cancel couldn't terminate a RUNNING job (state out
    of sync). Benign for a per-configuration supersede, but for a *global*
    cancel (``configuration is None``, used by ``reset_build_env``) it means a
    job is still running that the clean-all wipe would race — so re-raise there
    rather than wipe over it.
    """
    is_global = configuration is None
    to_cancel = [
        j.job_id
        for j in controller.state.active_jobs()
        if j.job_id not in exclude_job_ids and (is_global or j.configuration == configuration)
    ]
    for job_id in to_cancel:
        try:
            await controller.cancel(job_id=job_id)
        except (ValueError, RuntimeError):
            # Status flipped under us — the runner finalised the job
            # mid-iteration, or state is out of sync.
            if is_global:
                raise
        except CommandError as exc:
            # Already terminal (INVALID_ARGS) or already gone (NOT_FOUND) —
            # e.g. a chain's compile cascade-cancelled its held upload before
            # we reached it. Re-raise anything else so a real failure surfaces.
            if exc.code not in (ErrorCode.INVALID_ARGS, ErrorCode.NOT_FOUND):
                raise
