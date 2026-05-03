"""End-to-end coverage for ``FirmwareController.compile_bulk``.

The handler runs in two phases:

1. **Boundary validation** — ``_validate_configurations_boundary``
   checks every configuration up front in a single executor task
   and raises ``CommandError(INVALID_ARGS)`` on the first
   traversal-shaped or empty entry. Bad input rejects the *whole*
   batch, not partial — a typo in one of N configs is something
   the caller wants to know about, not have masked by partial
   success.
2. **Per-device enqueue** — for each pre-validated config, build a
   ``COMPILE`` job and enqueue it. A ``CommandError`` raised by
   ``_enqueue`` here is *transient state* (rename-lock conflict,
   not bad input) and the loop logs + skips the affected entry,
   queueing the rest.

The two-phase contract is the interesting part: validation
errors fail-fast on the entire batch, runtime errors fail-soft
per-entry. ``test_rename_lock.py`` covers the rename-lock path
end-to-end via ``install_bulk``; this file pins the validation
phase and the structural contract for ``compile_bulk``
specifically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, EventType, FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import (
    FirmwareControllerFactory,
    capture_firmware_events,
)


@pytest.mark.asyncio
async def test_compile_bulk_returns_queued_jobs_for_every_config(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Happy path: one ``COMPILE`` job per configuration, all ``QUEUED``.

    Pin the order, type, and status so a future refactor that
    iterates the input list out of order, drops entries silently,
    or defaults to a different job type fails the assertion.
    """
    for name in ("kitchen.yaml", "garage.yaml", "office.yaml"):
        (tmp_path / name).write_text("")
    controller = firmware_controller_factory(with_queue=True)

    jobs = await controller.compile_bulk(
        configurations=["kitchen.yaml", "garage.yaml", "office.yaml"],
    )

    assert [j.configuration for j in jobs] == ["kitchen.yaml", "garage.yaml", "office.yaml"]
    assert all(j.job_type == JobType.COMPILE for j in jobs)
    assert all(j.status == JobStatus.QUEUED for j in jobs)


@pytest.mark.asyncio
async def test_compile_bulk_rejects_whole_batch_on_traversal_entry(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A traversal payload anywhere in the list rejects the whole batch.

    Bad input is bad input — a typo in one of N configs is
    something the caller wants to know about, not have masked
    by partial success. Pin both halves: the validator raises
    ``CommandError(INVALID_ARGS)``, AND no jobs land in
    ``self._jobs`` (the validator runs *before* the per-entry
    enqueue loop, so a single bad entry must keep every other
    entry's job from being created too).
    """
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "garage.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.compile_bulk(
            configurations=["kitchen.yaml", "../etc/passwd", "garage.yaml"],
        )
    assert exc.value.code == ErrorCode.INVALID_ARGS
    # Critical: zero jobs created. The validator phase must run
    # to completion before any enqueue work, so a partial batch
    # with the valid head queued and the bad entry erroring would
    # be a regression.
    assert await controller.get_jobs() == []


@pytest.mark.asyncio
async def test_compile_bulk_rejects_whole_batch_on_empty_entry(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """An empty-string configuration rejects the whole batch.

    Same fail-fast contract as the traversal case. Empty
    strings are explicitly rejected by
    ``_sync_validate_configuration_boundary`` (only
    ``reset_build_env`` legitimately uses them, and it bypasses
    the validator entirely).
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.compile_bulk(configurations=["kitchen.yaml", ""])
    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert await controller.get_jobs() == []


@pytest.mark.asyncio
async def test_compile_bulk_skips_entries_with_enqueue_command_error(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A ``CommandError`` from ``_enqueue`` skips that entry but keeps going.

    Distinct from the validation phase: by the time we reach the
    per-entry enqueue loop, every ``configuration`` has passed the
    boundary validator. A ``CommandError`` raised from
    ``_enqueue`` (canonical case: ``_check_rename_lock`` rejecting
    a job whose configuration overlaps with an in-flight rename)
    is transient state, not bad input — drop the affected entry,
    log it, queue the rest. ``test_rename_lock.py`` covers the
    rename-lock path end-to-end via ``install_bulk``; this is the
    structural contract for ``compile_bulk``: the loop's
    ``except CommandError: continue`` arm actually runs and the
    surviving jobs land.
    """
    for name in ("kitchen.yaml", "locked.yaml", "office.yaml"):
        (tmp_path / name).write_text("")
    controller = firmware_controller_factory(with_queue=True)

    real_enqueue = controller._enqueue

    async def _flaky_enqueue(job: FirmwareJob) -> FirmwareJob:
        if job.configuration == "locked.yaml":
            raise CommandError(ErrorCode.INVALID_ARGS, "rename in flight on locked.yaml")
        return await real_enqueue(job)

    controller._enqueue = _flaky_enqueue  # type: ignore[method-assign]

    jobs = await controller.compile_bulk(
        configurations=["kitchen.yaml", "locked.yaml", "office.yaml"],
    )

    # The skipped entry is dropped from the result list; the
    # other two are queued normally.
    queued_configs = [j.configuration for j in jobs]
    assert queued_configs == ["kitchen.yaml", "office.yaml"]
    # The skipped job's ``_create_job`` call still wrote it into
    # the in-memory job map (the rename-lock check fires inside
    # ``_enqueue``, *after* ``_create_job`` registers the job).
    # That's the production contract — skipping the enqueue
    # leaves a stranded ``QUEUED`` entry visible via ``get_jobs``.
    # Pin it so a future refactor that swaps the order surfaces
    # here.
    assert "locked.yaml" in {j.configuration for j in await controller.get_jobs()}


@pytest.mark.asyncio
async def test_compile_bulk_empty_input_returns_empty_list(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """An empty ``configurations`` list returns ``[]`` without raising.

    Frontend's "select all and compile" can produce an empty
    list when the user selected nothing — surface a clean empty
    result rather than a confusing error.
    """
    controller = firmware_controller_factory(with_queue=True)

    jobs = await controller.compile_bulk(configurations=[])

    assert jobs == []
    assert await controller.get_jobs() == []


@pytest.mark.asyncio
async def test_compile_bulk_fires_job_queued_per_successful_entry(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``JOB_QUEUED`` fires exactly once per queued job — no double-fire, no skip.

    The all-jobs panel adds a row per ``JOB_QUEUED``; without
    one event per job the panel goes silent for the missing
    entry until the first ``JOB_OUTPUT`` arrives.
    """
    for name in ("kitchen.yaml", "garage.yaml"):
        (tmp_path / name).write_text("")
    controller = firmware_controller_factory(with_queue=True)
    captured = capture_firmware_events(controller, EventType.JOB_QUEUED)

    jobs = await controller.compile_bulk(configurations=["kitchen.yaml", "garage.yaml"])

    assert [event.data["job"] for event in captured] == jobs
