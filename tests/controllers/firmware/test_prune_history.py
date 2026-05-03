"""End-to-end coverage for ``FirmwareController._prune_history``.

The prune helper runs after every terminal-state transition (and
on cancel / clear) to keep ``self._jobs`` from growing unbounded.
The classification logic is a three-way fork — active / primary
terminal / aux terminal — and the aux branch
(``aux.append(job)``) was uncovered: the existing end-to-end
suite rarely overflows the aux pool because clean and
reset_build_env are uncommon paths.

These tests drive the public API: submit jobs via ``clean`` and
``compile``, then cancel them via ``cancel``. The QUEUED-branch
of ``cancel`` flips the job to CANCELLED and invokes
``_prune_history``. End-to-end shape so a refactor that moves
the prune call to a different site or changes cap-handling can't
slip past the tests.
"""

from __future__ import annotations

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.firmware.constants import _MAX_AUX_TERMINAL_JOBS
from esphome_device_builder.models import JobStatus, JobType

from .conftest import FirmwareControllerFactory


async def _submit_cleans(
    controller: FirmwareController, count: int, *, prefix: str = "device"
) -> list[str]:
    """Submit *count* clean jobs through ``controller.clean`` and return their ids."""
    job_ids: list[str] = []
    for i in range(count):
        job = await controller.clean(configuration=f"{prefix}-{i}.yaml")
        job_ids.append(job.job_id)
    return job_ids


async def _cancel_all(controller: FirmwareController, job_ids: list[str]) -> None:
    """Cancel every QUEUED job — drives each through the prune path in turn."""
    for job_id in job_ids:
        await controller.cancel(job_id=job_id)


async def test_cancelling_aux_jobs_below_cap_keeps_them_all(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Aux-terminal jobs below the cap all survive after the prune.

    Submit a handful of clean jobs through the public API and
    cancel them — each cancel transitions the job to CANCELLED
    and invokes ``_prune_history``. Below the cap nothing is
    evicted.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    under_cap = _MAX_AUX_TERMINAL_JOBS - 1
    job_ids = await _submit_cleans(controller, under_cap)

    await _cancel_all(controller, job_ids)

    surviving = [j for j in controller._jobs.values() if j.job_type == JobType.CLEAN]
    assert len(surviving) == under_cap
    assert all(j.status == JobStatus.CANCELLED for j in surviving)


async def test_cancelling_aux_jobs_over_cap_drops_oldest(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Cancelling more aux jobs than the cap allows evicts the oldest entries.

    Pins the ``aux.append(job)`` + ``aux[:_MAX_AUX_TERMINAL_JOBS]``
    chain end-to-end. Without the cap, a user spamming
    ``firmware/clean`` could grow ``self._jobs`` unbounded —
    clean jobs don't get the per-configuration dedup the primary
    pool uses, so a separate cap is the only thing keeping the
    pool bounded.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    overflow = 3
    total = _MAX_AUX_TERMINAL_JOBS + overflow
    job_ids = await _submit_cleans(controller, total)

    await _cancel_all(controller, job_ids)

    surviving_ids = set(controller._jobs.keys())
    assert len(surviving_ids) == _MAX_AUX_TERMINAL_JOBS
    # Newest-first ordering: the most-recently submitted ids
    # survive, the oldest ``overflow`` are evicted.
    assert surviving_ids == set(job_ids[overflow:])


async def test_aux_overflow_does_not_evict_compile_jobs(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A compile that lands in the primary pool survives an aux-pool overflow.

    Submit one compile, cancel it (lands in primary), then spam
    enough cancelled cleans to overflow the aux pool. The compile
    survives because aux + primary are independent pools.

    Pins the ``primary.append(job)`` / ``aux.append(job)`` fork.
    A regression that conflated the two pools (single shared cap)
    would silently evict the recent compile under heavy clean
    activity.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    compile_job = await controller.compile(configuration="kitchen.yaml")
    await controller.cancel(job_id=compile_job.job_id)

    clean_ids = await _submit_cleans(controller, _MAX_AUX_TERMINAL_JOBS + 5)
    await _cancel_all(controller, clean_ids)

    # The compile (primary) survived the aux flood.
    assert compile_job.job_id in controller._jobs
    # Aux pool got capped.
    aux_kept = [j for j in controller._jobs.values() if j.job_type == JobType.CLEAN]
    assert len(aux_kept) == _MAX_AUX_TERMINAL_JOBS


async def test_active_aux_job_survives_aux_overflow(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A still-QUEUED clean job is never pruned regardless of aux overflow.

    Active jobs (queued/running) go to a third bucket which has
    no cap. Pin the early ``status not in terminal_states``
    branch so a regression that lumped queued cleans in with
    terminal aux can't evict an in-flight job.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)

    # One queued job we deliberately leave alive.
    queued_job = await controller.clean(configuration="device-queued.yaml")

    # Overflow the aux pool with cancelled cleans.
    overflow_ids = await _submit_cleans(
        controller, _MAX_AUX_TERMINAL_JOBS + 2, prefix="device-overflow"
    )
    await _cancel_all(controller, overflow_ids)

    # Queued job is still in _jobs.
    assert queued_job.job_id in controller._jobs
    assert controller._jobs[queued_job.job_id].status == JobStatus.QUEUED
    # Terminal aux pool capped (queued job doesn't count toward the cap).
    terminal_cleans = [
        j
        for j in controller._jobs.values()
        if j.job_type == JobType.CLEAN and j.status != JobStatus.QUEUED
    ]
    assert len(terminal_cleans) == _MAX_AUX_TERMINAL_JOBS


async def test_aux_pool_keeps_repeated_cleans_against_same_configuration(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Aux jobs against the same configuration all survive (within the cap).

    The primary pool collapses to one entry per configuration so
    the recent-jobs panel doesn't fill with repeated compiles of
    the same device. Aux is intentionally NOT deduped — repeated
    clean runs against the same device are a meaningful diagnostic
    signal ("why is this device needing constant cleans?") and
    the per-pool cap already bounds memory.

    Repeated ``clean`` calls for the same configuration don't pile
    up as QUEUED — supersede cancels each prior one when the next
    arrives, which is itself a prune trigger. The auto-cancelled
    jobs land in the aux pool; the latest one stays QUEUED. Pin
    that all of them are still present (no per-configuration
    collapse).
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    job_ids: list[str] = []
    for _ in range(3):
        job = await controller.clean(configuration="kitchen.yaml")
        job_ids.append(job.job_id)

    # All three survived — supersede cancelled the first two (now
    # in the aux pool) and the third is still QUEUED. None were
    # collapsed by configuration.
    assert set(job_ids).issubset(controller._jobs.keys())
    statuses = {controller._jobs[jid].status for jid in job_ids}
    assert statuses == {JobStatus.CANCELLED, JobStatus.QUEUED}
