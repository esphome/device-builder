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
  would iterate ``self._jobs.values()``, find the new
  ``QUEUED`` entry, and immediately cancel it.

Drives through public API only — submit via ``compile`` /
``reset_build_env``, assert via ``get_jobs``. The supersede
happens as a side effect of the second ``_enqueue``; tests
don't call ``_supersede_active_jobs`` directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.models import FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_resubmit_does_not_cancel_itself(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The new submission's own ``QUEUED`` entry is excluded from supersede.

    Without the ``exclude_job_id`` guard,
    ``_supersede_active_jobs`` would iterate ``self._jobs.values()``,
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


@pytest.mark.asyncio
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
    # Simulate the runner having picked it up.
    in_flight = controller._jobs[first.job_id]
    in_flight.status = JobStatus.RUNNING
    controller._current_job = in_flight

    second = await controller.compile(configuration="kitchen.yaml")

    # Cancel intent recorded for the predecessor — the runner's
    # ``finally`` would convert this into terminal CANCELLED on
    # the next turn (not exercised here; ``_terminate_current_process``
    # is the AsyncMock from ``with_terminate=True``).
    assert first.job_id in controller._cancel_requested
    controller._terminate_current_process.assert_awaited()
    # Second submission queued normally.
    assert controller._jobs[second.job_id].status == JobStatus.QUEUED


@pytest.mark.asyncio
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
    controller = firmware_controller_factory(with_queue=True)
    # Seed historical entries directly.
    historical: list[FirmwareJob] = []
    for status, job_id in [
        (JobStatus.COMPLETED, "h-completed"),
        (JobStatus.FAILED, "h-failed"),
        (JobStatus.CANCELLED, "h-cancelled"),
    ]:
        job = FirmwareJob(
            job_id=job_id,
            configuration="kitchen.yaml",
            job_type=JobType.COMPILE,
            status=status,
        )
        controller._jobs[job_id] = job
        historical.append(job)

    fresh = await controller.compile(configuration="kitchen.yaml")

    jobs = {j.job_id: j for j in await controller.get_jobs()}
    # Historical entries kept their original terminal status.
    for job in historical:
        assert jobs[job.job_id].status == job.status
    # Fresh submission queued normally.
    assert jobs[fresh.job_id].status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_supersede_does_not_run_for_empty_configuration(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``reset_build_env`` (empty configuration) skips the supersede path.

    ``_enqueue`` only runs supersede when ``job.configuration``
    is truthy. ``reset_build_env`` is the only handler that
    queues with an empty configuration — without the guard,
    ``_supersede_active_jobs`` would iterate every job whose
    configuration matches ``""`` (i.e. only other reset jobs)
    and cancel them, which is fine in isolation but the guard
    makes the intent explicit.

    Pin that two consecutive reset_build_env calls don't
    supersede each other (since both have ``configuration=""``
    AND the guard skips the call entirely, neither path can
    cancel the other).
    """
    controller = firmware_controller_factory(with_queue=True)
    first = await controller.reset_build_env()
    second = await controller.reset_build_env()

    jobs = {j.job_id: j for j in await controller.get_jobs()}
    assert jobs[first.job_id].status == JobStatus.QUEUED
    assert jobs[second.job_id].status == JobStatus.QUEUED
