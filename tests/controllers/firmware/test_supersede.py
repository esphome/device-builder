"""End-to-end coverage for the supersede-on-resubmit flow.

When the user re-submits a firmware operation for a device that
already has a job in flight, ``_supersede_active_jobs`` cancels
the predecessor so the all-jobs panel only shows one active
entry per device. The flow is wired into ``_enqueue`` (after
``JOB_QUEUED`` fires for the new job, before persistence), so
the user-visible contract is:

- Submit two compiles for the same configuration in sequence:
  the first lands as ``CANCELLED``, the second as ``QUEUED``.
- Submit two compiles for *different* configurations: both
  stay ``QUEUED`` (supersede is per-configuration).
- A running job for the same configuration gets cancelled
  the same way (the runner's ``_terminate_current_process``
  fires).
- The ``exclude_job_id`` guard keeps the new submission from
  cancelling itself — without it, ``_supersede_active_jobs``
  would iterate ``self.state.jobs.values()``, find the new
  ``QUEUED`` entry, and immediately cancel it.

Drives through public API only — submit via ``compile`` /
``reset_build_env``, assert via ``get_jobs``. The supersede
happens as a side effect of the second ``_enqueue``; tests
don't call ``_supersede_active_jobs`` directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers.firmware import factories
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory


async def test_resubmit_cancels_previous_queued_job_for_same_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Re-submitting a compile for the same config cancels the predecessor.

    User flow: click "Compile" twice on the same device. The
    second click supersedes the first so the manage-tasks panel
    only shows one in-flight job per device. Pin both halves —
    the first ends up ``CANCELLED``, the second ``QUEUED``.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    first = await controller.compile(configuration="kitchen.yaml")
    second = await controller.compile(configuration="kitchen.yaml")

    jobs = {j.job_id: j for j in await controller.get_jobs()}
    assert jobs[first.job_id].status == JobStatus.CANCELLED
    assert jobs[second.job_id].status == JobStatus.QUEUED


async def test_resubmit_does_not_cancel_jobs_for_different_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Supersede is scoped to the matching configuration only.

    Two parallel compile requests for different devices
    shouldn't fight each other — each device keeps its own
    queued job. Pin the per-config scoping so a refactor that
    accidentally widens the filter (e.g. drops the
    ``configuration ==`` check) shows up here.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "garage.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    kitchen = await controller.compile(configuration="kitchen.yaml")
    garage = await controller.compile(configuration="garage.yaml")

    jobs = {j.job_id: j for j in await controller.get_jobs()}
    assert jobs[kitchen.job_id].status == JobStatus.QUEUED
    assert jobs[garage.job_id].status == JobStatus.QUEUED


async def test_resubmit_does_not_cancel_itself(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The new submission's own ``QUEUED`` entry is excluded from supersede.

    Without the ``exclude_job_id`` guard,
    ``_supersede_active_jobs`` would iterate ``self.state.jobs.values()``,
    find the new submission's own entry (already in
    ``_jobs`` by the time supersede runs), and cancel it
    along with the predecessor — leaving the user with no
    active job at all. Pin the guard.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    first = await controller.compile(configuration="kitchen.yaml")

    # First submission with no predecessor — only the new job
    # is in ``_jobs``. If supersede mishandled ``exclude_job_id``
    # the new job would land ``CANCELLED``.
    jobs = {j.job_id: j for j in await controller.get_jobs()}
    assert jobs[first.job_id].status == JobStatus.QUEUED


async def test_resubmit_cancels_running_predecessor(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A ``RUNNING`` predecessor gets cancelled too — runner is signalled.

    The same supersede policy applies whether the predecessor
    is queued or running. For a running job, ``cancel`` records
    intent in ``_cancel_requested`` and calls
    ``_terminate_current_process`` (which signals the
    subprocess); the runner's ``finally`` finalises with
    status ``CANCELLED`` on the next turn.

    This test simulates the runner being mid-build by mutating
    ``_jobs[id].status`` directly + setting ``_current_job``,
    same approach as the persistence test (no public API for
    "make the runner mid-build" without a real ``esphome``).
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    first = await controller.compile(configuration="kitchen.yaml")
    # Simulate the runner having picked it up. ``status`` is set
    # on the live job object; there's no public API for putting a
    # job into RUNNING without spawning a real ``esphome`` (same
    # justified seam as ``test_persistence.py``'s RUNNING-carryover
    # test).
    first.status = JobStatus.RUNNING
    controller.state.compile_lane.current_job = first

    second = await controller.compile(configuration="kitchen.yaml")

    # Cancel intent recorded for the predecessor — the runner's
    # ``finally`` would convert this into terminal CANCELLED on
    # the next turn (not exercised here; ``_terminate_current_process``
    # is the AsyncMock from ``with_terminate=True``).
    assert first.job_id in controller.state.cancel_requested
    controller._terminate_current_process.assert_awaited()
    # Second submission queued normally.
    jobs = {j.job_id: j for j in await controller.get_jobs()}
    assert jobs[second.job_id].status == JobStatus.QUEUED


async def test_resubmit_does_not_cancel_terminal_jobs_for_same_config(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Already-terminal jobs (history) for the same config aren't re-cancelled.

    A user who completed a compile yesterday and re-runs it
    today shouldn't have the historical ``COMPLETED`` entry
    flipped to ``CANCELLED``. Supersede only targets active
    (``QUEUED`` / ``RUNNING``) entries; terminal ones are
    history.

    Three flavours via direct seeding (no public API to land
    a job in ``COMPLETED`` / ``FAILED`` status without
    spawning a real ``esphome``).
    """
    (tmp_path / "kitchen.yaml").write_text("")
    # Seed historical entries through the factory's ``*jobs``
    # arg — no public API to land a job in COMPLETED / FAILED /
    # CANCELLED without spawning a real ``esphome``.
    historical: list[FirmwareJob] = [
        FirmwareJob(
            job_id=job_id,
            configuration="kitchen.yaml",
            job_type=JobType.COMPILE,
            status=status,
        )
        for status, job_id in [
            (JobStatus.COMPLETED, "h-completed"),
            (JobStatus.FAILED, "h-failed"),
            (JobStatus.CANCELLED, "h-cancelled"),
        ]
    ]
    controller = firmware_controller_factory(*historical, with_queue=True)

    fresh = await controller.compile(configuration="kitchen.yaml")

    jobs = {j.job_id: j for j in await controller.get_jobs()}
    # Historical entries kept their original terminal status.
    for job in historical:
        assert jobs[job.job_id].status == job.status
    # Fresh submission queued normally.
    assert jobs[fresh.job_id].status == JobStatus.QUEUED


async def test_second_reset_build_env_cancels_the_first(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A second clean-all cancels the first still-pending reset.

    ``reset_build_env`` cancels every active job before queueing (the wipe
    trashes the whole build tree); an earlier queued reset is one of them, so
    only the latest survives. The cancellation comes from the explicit
    ``cancel_all_active_jobs`` call, not ``_enqueue``'s supersede — which still
    skips the empty ``configuration`` reset jobs queue with.
    """
    controller = firmware_controller_factory(with_queue=True)
    first = await controller.reset_build_env()
    second = await controller.reset_build_env()

    jobs = {j.job_id: j for j in await controller.get_jobs()}
    assert jobs[first.job_id].status == JobStatus.CANCELLED
    assert jobs[second.job_id].status == JobStatus.QUEUED


async def test_supersede_reraises_unexpected_cancel_error(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A non-benign CommandError from cancel propagates out of supersede.

    Supersede swallows only the already-terminal / already-gone cases
    (INVALID_ARGS / NOT_FOUND); any other typed failure must surface rather
    than silently leave a superseded job active.
    """
    victim = FirmwareJob(
        job_id="v1",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.QUEUED,
    )
    controller = firmware_controller_factory(victim, with_queue=True)

    async def _boom(*, job_id: str) -> None:
        raise CommandError(ErrorCode.INTERNAL_ERROR, "boom")

    controller.cancel = _boom  # type: ignore[method-assign]

    with pytest.raises(CommandError) as exc:
        await factories.supersede_active_jobs(controller, "kitchen.yaml", exclude_job_ids=set())

    assert exc.value.code == ErrorCode.INTERNAL_ERROR


async def test_supersede_swallows_runtime_error_from_cancel(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A state-out-of-sync RuntimeError from cancel is swallowed, not propagated.

    cancel raises RuntimeError when a RUNNING job isn't the active subprocess;
    supersede treats that (and ValueError) as a benign mid-iteration flip and
    keeps going rather than aborting the new submission.
    """
    victim = FirmwareJob(
        job_id="v1",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.QUEUED,
    )
    controller = firmware_controller_factory(victim, with_queue=True)

    async def _boom(*, job_id: str) -> None:
        raise RuntimeError("state out of sync")

    controller.cancel = _boom  # type: ignore[method-assign]

    # Must not raise.
    await factories.supersede_active_jobs(controller, "kitchen.yaml", exclude_job_ids=set())


async def test_cancel_all_active_jobs_reraises_runtime_error(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The global cancel (clean-all) re-raises a RuntimeError from cancel.

    A RuntimeError means a RUNNING job couldn't be terminated; wiping the build
    tree while it runs would corrupt it, so reset must fail loudly rather than
    swallow it the way a per-configuration supersede does.
    """
    victim = FirmwareJob(
        job_id="v1",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
    )
    controller = firmware_controller_factory(victim, with_queue=True)

    async def _boom(*, job_id: str) -> None:
        raise RuntimeError("state out of sync")

    controller.cancel = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await factories.cancel_all_active_jobs(controller, exclude_job_ids=set())


async def test_reset_build_env_rolls_back_its_job_when_cancel_reraises(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A re-raising global sweep leaves no orphaned RESET job behind.

    An orphaned QUEUED reset is an active job: it wedges the upload lane via
    ``upload_blocked`` and runs a clean-all on restart. Roll it back instead.
    """
    victim = FirmwareJob(
        job_id="v1",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
    )
    controller = firmware_controller_factory(victim, with_queue=True)

    async def _boom(*, job_id: str) -> None:
        raise RuntimeError("state out of sync")

    controller.cancel = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await controller.reset_build_env()

    assert not any(
        job.job_type is JobType.RESET_BUILD_ENV for job in controller.state.jobs.values()
    )
