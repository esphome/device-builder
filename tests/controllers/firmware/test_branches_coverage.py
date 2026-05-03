"""Coverage for the smaller load-bearing branches in ``firmware/controller.py``.

Each test pins one specific branch the per-feature suites either
skip or cover only via a deeper helper. Short, surgical tests so
when a branch they protect regresses, the failure clearly names
the missing behaviour.

Surfaces touched here:

- **Public submission**: ``firmware/rename`` happy path (the
  rename-lock suite covers conflict cases but nothing pins the
  enqueue path itself).
- **Stream / follower wiring**: ``follow_job`` raises ValueError
  for an unknown job id, ``follow_jobs`` early-returns when
  ``client`` is None.
- **Runner internals**: queue runner skips a CANCELLED job
  without spawning a subprocess, ``_terminate_current_process``
  is a no-op when no process is bound.
- **Command building**: ``_build_command`` for ``RENAME`` appends
  ``new_name`` as a positional arg.
- **Prune-history dedup**: same-configuration primary-pool entries
  collapse to the newest.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    ErrorCode,
    FirmwareJob,
    JobStatus,
    JobType,
)
from tests.controllers.firmware.conftest import (
    FirmwareControllerFactory,
)


def _job(
    job_id: str,
    configuration: str,
    job_type: JobType,
    *,
    status: JobStatus = JobStatus.COMPLETED,
    new_name: str = "",
    created_at: str = "",
) -> FirmwareJob:
    return FirmwareJob(
        job_id=job_id,
        configuration=configuration,
        job_type=job_type,
        status=status,
        new_name=new_name,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# firmware/rename — public submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_returns_queued_rename_job(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Happy path: handler returns a ``QUEUED`` ``RENAME`` job carrying ``new_name``.

    The rename-lock suite covers conflict cases but nothing pins
    the enqueue itself — a regression that swapped the job_type
    or dropped ``new_name`` would still pass every lock-policy
    test (none of them inspect the resulting job's shape).
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.RENAME
    assert job.configuration == "kitchen.yaml"
    assert job.new_name == "livingroom"


@pytest.mark.asyncio
async def test_rename_rejects_when_target_filename_already_exists(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A pre-existing ``<new_name>.yaml`` blocks the rename with INVALID_ARGS.

    ``esphome rename`` does NOT check for collisions — it
    blindly writes the new YAML and OTA-installs it. A
    direct WS client that bypassed the controller-layer check
    would silently overwrite an unrelated device's config and
    flash this device's firmware to it. Pin the handler-side
    check so a refactor that dropped it can't silently make
    that path reachable again.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "livingroom.yaml").write_text("")  # pre-existing target

    with pytest.raises(CommandError) as excinfo:
        await controller.rename(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "livingroom.yaml" in excinfo.value.message


# ---------------------------------------------------------------------------
# follow_job / follow_jobs — stream wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_job_raises_value_error_for_unknown_job_id(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """An unknown ``job_id`` raises ``ValueError`` before any stream work.

    The WS layer translates ``ValueError`` into a typed error
    response; pinning the precise exception keeps the
    "Job not found" message reaching the dashboard's task panel
    instead of a generic "Command failed".
    """
    controller = firmware_controller_factory(with_settings=False)

    with pytest.raises(ValueError, match="Job not found: ghost-id"):
        await controller.follow_job(job_id="ghost-id", client=MagicMock())


@pytest.mark.asyncio
async def test_follow_jobs_returns_immediately_when_client_is_none(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``follow_jobs`` is a no-op when ``client`` is missing.

    The WS dispatcher passes ``client=None`` for in-process
    callers (e.g. the WS test harness driving the handler
    without a live socket). Without the early return, the
    handler would later iterate ``self._jobs`` and call
    ``client.send_event`` on ``None`` — an attribute error,
    not a clean shape mismatch.
    """
    controller = firmware_controller_factory()
    controller._jobs = {
        "j1": _job("j1", "kitchen.yaml", JobType.COMPILE, status=JobStatus.COMPLETED),
    }

    # Should return without raising — no iteration, no send_event.
    result = await controller.follow_jobs(client=None, snapshot=True)
    assert result is None


# ---------------------------------------------------------------------------
# Queue runner — CANCELLED-skip and missing-process branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_queue_skips_cancelled_jobs_without_spawning(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A CANCELLED job pulled from the queue is skipped, not executed.

    A user can cancel a QUEUED job via ``firmware/cancel``; the
    cancel handler flips the job's status to CANCELLED but doesn't
    pluck it out of the queue (the queue is FIFO with no remove API).
    The runner's first action on every dequeue is the
    ``status == CANCELLED`` check — without it the runner would
    spawn a real subprocess for a job the user already gave up on.
    """
    controller = firmware_controller_factory()
    controller._queue = asyncio.Queue()
    cancelled = _job("j1", "kitchen.yaml", JobType.COMPILE, status=JobStatus.CANCELLED)
    await controller._queue.put(cancelled)

    spawned = False

    async def _spy_execute(_job: FirmwareJob) -> None:
        nonlocal spawned
        spawned = True

    controller._execute_job = _spy_execute  # type: ignore[method-assign]

    runner = asyncio.create_task(controller._run_queue())
    # Give the runner a chance to dequeue + skip + return for next get.
    for _ in range(20):
        await asyncio.sleep(0)
        if controller._queue.empty():
            break
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner

    assert spawned is False


@pytest.mark.asyncio
async def test_terminate_current_process_no_op_when_no_process(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``_terminate_current_process`` returns cleanly when no process is bound.

    The cancel handler always calls ``_terminate_current_process``
    after flipping the status — but the QUEUED-cancel path runs
    before the runner has spawned anything, so the controller's
    ``_current_process`` is still ``None``. Pin the early return
    so a regression that fell through to ``terminate_subtree_*``
    against ``None`` would surface as a hard error here.
    """
    controller = firmware_controller_factory()
    controller._current_process = None
    controller._current_job = None

    # Should return without raising; no process to terminate.
    await controller._terminate_current_process()


# ---------------------------------------------------------------------------
# _build_command — RENAME branch
# ---------------------------------------------------------------------------


def test_build_command_for_rename_appends_new_name_positional() -> None:
    """``RENAME`` appends ``new_name`` as a trailing positional arg.

    ``esphome rename <yaml> <new_name>`` is the CLI shape;
    without the trailing positional the CLI errors out before
    touching the YAML, and the dashboard would report
    "rename failed" with no actionable hint. Pin the arg order.
    """
    controller = FirmwareController.__new__(FirmwareController)
    controller._esphome_cmd = ["esphome"]
    controller._db = MagicMock()
    controller._db.devices = None

    cmd = controller._build_command(JobType.RENAME, "kitchen.yaml", port="", new_name="livingroom")

    assert cmd == [
        "esphome",
        "--dashboard",
        "rename",
        "kitchen.yaml",
        "livingroom",
    ]


# ---------------------------------------------------------------------------
# _prune_history — primary-pool dedup by configuration
# ---------------------------------------------------------------------------


def test_prune_history_collapses_primary_jobs_to_newest_per_configuration(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Two terminal compiles for the same configuration collapse to the newest.

    The recent-jobs panel would otherwise fill up with repeated
    compile entries for one device, pushing legitimate older
    runs out of the cap window. The aux pool deliberately doesn't
    dedupe (clean / reset_build_env runs are diagnostic signals);
    primary jobs do, because re-compiling the same config is
    routine and not interesting on its own.
    """
    base = datetime(2026, 5, 1, tzinfo=UTC)
    older = _job(
        "old",
        "kitchen.yaml",
        JobType.COMPILE,
        status=JobStatus.COMPLETED,
        created_at=base.isoformat(),
    )
    newer = _job(
        "new",
        "kitchen.yaml",
        JobType.COMPILE,
        status=JobStatus.COMPLETED,
        created_at=(base + timedelta(minutes=5)).isoformat(),
    )
    controller = firmware_controller_factory(older, newer, with_settings=False)

    controller._prune_history()

    surviving_ids = set(controller._jobs.keys())
    assert surviving_ids == {"new"}
