"""Direct coverage for ``FirmwareController._prune_history``.

The prune helper runs after every terminal-state transition (and
on cancel / clear) to keep ``self._jobs`` from growing unbounded.
The classification logic is a three-way fork — active / primary
terminal / aux terminal — and the aux branch is the one the
end-to-end tests rarely hit because clean and reset_build_env
are uncommon paths.

These tests drive ``_prune_history`` directly with a hand-crafted
``self._jobs`` so each branch lands cleanly without having to
spin up real subprocesses to drive the controller through enough
clean / reset cycles to overflow the aux pool.

Three contracts pinned:

1. Active jobs are never pruned regardless of pool sizes.
2. Aux-terminal jobs (clean, reset_build_env) sort newest-first
   and cap at ``_MAX_AUX_TERMINAL_JOBS`` — this is the branch the
   end-to-end suite missed.
3. Aux and primary pools are independent — overflowing one
   doesn't evict from the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from esphome_device_builder.controllers.firmware.constants import (
    _MAX_AUX_TERMINAL_JOBS,
    _MAX_PRIMARY_TERMINAL_JOBS,
)
from esphome_device_builder.models import FirmwareJob, JobStatus, JobType

from .conftest import FirmwareControllerFactory


def _terminal_job(
    job_id: str, *, job_type: JobType, configuration: str = "kitchen.yaml", offset_s: int = 0
) -> FirmwareJob:
    """Build a terminal ``FirmwareJob`` with a deterministic ``created_at``.

    ``offset_s`` controls the relative ordering — older jobs use a
    smaller offset, so the prune's "newest-first" sort is observable
    in the result.
    """
    return FirmwareJob(
        job_id=job_id,
        configuration=configuration,
        job_type=job_type,
        status=JobStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=offset_s),
    )


def test_prune_keeps_aux_jobs_under_the_cap(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Terminal aux jobs (clean / reset_build_env) below the cap all survive."""
    aux_jobs = [
        _terminal_job(f"clean-{i}", job_type=JobType.CLEAN, offset_s=i)
        for i in range(_MAX_AUX_TERMINAL_JOBS - 1)
    ]
    controller = firmware_controller_factory(*aux_jobs)

    controller._prune_history()

    assert len(controller._jobs) == _MAX_AUX_TERMINAL_JOBS - 1


def test_prune_caps_aux_pool_to_max_aux_terminal_jobs(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Overflowing the aux pool drops oldest entries and keeps the most recent N.

    Pin the ``aux.append(job)`` branch + the
    ``aux[:_MAX_AUX_TERMINAL_JOBS]`` cap. Without the cap the
    user could spam ``firmware/clean`` and grow ``self._jobs``
    unbounded since clean jobs don't get the per-configuration
    dedup the primary pool uses.
    """
    # _MAX_AUX_TERMINAL_JOBS + 3 jobs across two aux types, mixed
    # configurations to mirror real fleets where the user runs
    # clean across several devices and reset_build_env once or
    # twice.
    overflow = 3
    total = _MAX_AUX_TERMINAL_JOBS + overflow
    aux_jobs = [
        _terminal_job(
            f"aux-{i}",
            job_type=JobType.CLEAN if i % 2 == 0 else JobType.RESET_BUILD_ENV,
            configuration=f"device-{i}.yaml",
            offset_s=i,
        )
        for i in range(total)
    ]
    controller = firmware_controller_factory(*aux_jobs)

    controller._prune_history()

    surviving_ids = set(controller._jobs.keys())
    assert len(surviving_ids) == _MAX_AUX_TERMINAL_JOBS
    # Newest-first: the surviving ids are the last ``_MAX_AUX_TERMINAL_JOBS``
    # offsets (highest ``created_at`` values).
    expected_ids = {f"aux-{i}" for i in range(overflow, total)}
    assert surviving_ids == expected_ids


def test_prune_aux_pool_does_not_evict_from_primary(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The two pools are independent — overflowing aux doesn't touch compile/upload/install.

    Pin the ``primary.append(job)`` / ``aux.append(job)`` fork.
    A regression that conflated them (e.g. a single-pool cap)
    would silently evict a recent compile job under heavy clean
    activity.
    """
    primary = _terminal_job(
        "compile-recent",
        job_type=JobType.COMPILE,
        configuration="kitchen.yaml",
        offset_s=0,
    )
    aux_jobs = [
        _terminal_job(
            f"clean-{i}",
            job_type=JobType.CLEAN,
            configuration=f"device-{i}.yaml",
            offset_s=i + 1,
        )
        for i in range(_MAX_AUX_TERMINAL_JOBS + 5)
    ]
    controller = firmware_controller_factory(primary, *aux_jobs)

    controller._prune_history()

    # The compile (primary) survived the aux overflow.
    assert "compile-recent" in controller._jobs
    # Aux pool was capped.
    aux_kept = [j for j in controller._jobs.values() if j.job_type == JobType.CLEAN]
    assert len(aux_kept) == _MAX_AUX_TERMINAL_JOBS


def test_prune_keeps_active_jobs_regardless_of_aux_overflow(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Queued/running jobs survive even when the aux pool overflows.

    Active jobs go to a third bucket (``active``) that has no
    cap; pin the early ``status not in terminal_states`` branch
    so a regression that lumped them in with aux can't evict an
    in-flight clean from the runner's queue.
    """
    queued = FirmwareJob(
        job_id="clean-queued",
        configuration="device-x.yaml",
        job_type=JobType.CLEAN,
        status=JobStatus.QUEUED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    aux_terminal_overflow = [
        _terminal_job(
            f"clean-done-{i}",
            job_type=JobType.CLEAN,
            configuration=f"device-{i}.yaml",
            offset_s=i + 1,
        )
        for i in range(_MAX_AUX_TERMINAL_JOBS + 2)
    ]
    controller = firmware_controller_factory(queued, *aux_terminal_overflow)

    controller._prune_history()

    # Queued job kept — active bucket is uncapped.
    assert "clean-queued" in controller._jobs
    # Aux terminal pool still capped despite the queued job sharing the type.
    terminal_cleans = [
        j
        for j in controller._jobs.values()
        if j.job_type == JobType.CLEAN and j.status != JobStatus.QUEUED
    ]
    assert len(terminal_cleans) == _MAX_AUX_TERMINAL_JOBS


def test_prune_does_not_dedupe_aux_by_configuration(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Aux jobs against the same configuration all survive (within the cap).

    The primary pool collapses to one entry per configuration
    (newest wins) so the recent-jobs panel doesn't fill with
    repeated compiles of the same device. Aux is intentionally
    NOT deduped — repeated clean runs against the same device
    are a meaningful diagnostic signal ("why is this device
    needing constant cleans?") and the per-pool cap already
    bounds memory.
    """
    same_config_cleans = [
        _terminal_job(
            f"clean-{i}",
            job_type=JobType.CLEAN,
            configuration="kitchen.yaml",
            offset_s=i,
        )
        for i in range(3)
    ]
    controller = firmware_controller_factory(*same_config_cleans)

    controller._prune_history()

    # All three survive — no per-configuration collapse on the aux pool.
    assert len(controller._jobs) == 3


def test_prune_classification_pins_max_constants(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Sanity: the constants are reasonable and aux is meaningfully smaller than primary.

    The split-pool design only makes sense if aux is bounded
    much tighter than primary; if a refactor flattened them, the
    other tests in this module would still pass mechanically but
    the policy intent ("keep recent compiles, treat clean/reset
    as overflow noise") would be lost.
    """
    assert _MAX_AUX_TERMINAL_JOBS > 0
    assert _MAX_PRIMARY_TERMINAL_JOBS > _MAX_AUX_TERMINAL_JOBS
    # Avoid unused-fixture warning by exercising the factory.
    controller = firmware_controller_factory()
    assert controller._jobs == {}
