"""End-to-end coverage for ``FirmwareController.reset_build_env``.

The handler itself is a one-liner (``_create_job`` then
``_enqueue``), but the runner side (``_reset_build_env``) does
real filesystem work that no other test exercises:

- Removes each ``_RESET_BUILD_ENV_TARGETS`` directory under
  ``<config_dir>/.esphome/``.
- Streams progress lines through ``JOB_OUTPUT`` so the dashboard's
  follow_job dialog can show them.
- Skips targets that aren't present (``.esphome/external_components/``
  doesn't always exist) without aborting the rest.
- Honours cancellation between targets — ``rmtree`` itself isn't
  interruptible from another coroutine, so the user-visible
  contract is "stops before the next target if cancelled".
- Marks the job COMPLETED with ``exit_code=0`` and ``progress=100``
  on success, fires ``JOB_COMPLETED``.

This file pins both halves so a future split / refactor of the
job runner can't quietly drop the ``RESET_BUILD_ENV`` branch (it's
the only ``JobType`` that runs in-process; every other type
shells out via ``create_subprocess_exec``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.firmware.constants import _RESET_BUILD_ENV_TARGETS
from esphome_device_builder.models import EventType, FirmwareJob, JobStatus, JobType


def _controller(tmp_path: Path) -> FirmwareController:
    """Build a controller with a real config_dir and stubbed queue / persistence.

    Same shape as ``test_install._controller`` — bypass ``__init__``
    so we don't have to seed a full ``DeviceBuilder``, then attach
    the surface ``reset_build_env`` and ``_reset_build_env``
    actually touch (settings.config_dir for ``.esphome``,
    ``self._queue`` / ``self._persist_jobs`` /
    ``self._supersede_active_jobs`` for the enqueue path,
    ``self._db.bus`` for ``JOB_*`` broadcasts,
    ``self._cancel_requested`` for the mid-run cancel check).
    """
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()

    controller = FirmwareController.__new__(FirmwareController)
    controller._jobs = {}
    controller._queue = AsyncMock()
    controller._persist_jobs = AsyncMock()
    controller._supersede_active_jobs = AsyncMock()
    controller._cancel_requested = set()

    bus = MagicMock()
    bus.fire = MagicMock()
    controller._db = type("DB", (), {"settings": settings, "bus": bus})()
    return controller


# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_build_env_returns_queued_job_with_reset_type(tmp_path: Path) -> None:
    """Happy path: the handler returns a ``QUEUED`` job of type ``RESET_BUILD_ENV``.

    Pin both ``status`` and ``job_type`` so a future regression
    that defaults to ``COMPILE`` (or marks the new job RUNNING
    before the runner picks it up) shows up immediately. The
    frontend's job-table renders the row from these two fields.
    """
    controller = _controller(tmp_path)

    job = await controller.reset_build_env()

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.RESET_BUILD_ENV


@pytest.mark.asyncio
async def test_reset_build_env_uses_empty_configuration(tmp_path: Path) -> None:
    """``configuration`` is ``""`` — reset is global, not per-device.

    Documented contract in the handler's docstring; pinned here so
    a refactor that tries to attach a configuration (e.g. to satisfy
    a "configuration is required" linter rule) breaks the test
    rather than the runtime invariant the rename-lock /
    refresh-scheduling logic relies on
    (``test_helpers.test_names_touched_by_job_with_empty_configuration_is_empty``).
    """
    controller = _controller(tmp_path)

    job = await controller.reset_build_env()

    assert job.configuration == ""


@pytest.mark.asyncio
async def test_reset_build_env_registers_job_in_jobs_map(tmp_path: Path) -> None:
    """The new job lands in ``self._jobs`` so ``cancel`` / ``follow_job`` can find it."""
    controller = _controller(tmp_path)

    job = await controller.reset_build_env()

    assert controller._jobs[job.job_id] is job


@pytest.mark.asyncio
async def test_reset_build_env_fires_job_queued_after_enqueue(tmp_path: Path) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    Same ordering invariant as ``install`` /
    ``upload`` / ``compile``: the all-jobs panel keys off
    ``JOB_QUEUED`` to add a row when a new job lands, and a
    follower attaching on that signal must find the job already
    in the queue. If the broadcast preceded the put, a frontend
    that immediately calls ``follow_job`` could race the runner
    and miss the first lines.
    """
    parent = MagicMock()
    parent.queue.put = AsyncMock()
    parent.bus.fire = MagicMock()

    controller = _controller(tmp_path)
    controller._queue = parent.queue
    controller._db.bus = parent.bus

    job = await controller.reset_build_env()

    method_names = [name for name, _, _ in parent.method_calls]
    queued_idx = method_names.index("queue.put")
    fired_idx = method_names.index("bus.fire")
    assert queued_idx < fired_idx

    parent.bus.fire.assert_any_call(EventType.JOB_QUEUED, {"job": job})


@pytest.mark.asyncio
async def test_reset_build_env_accepts_arbitrary_kwargs(tmp_path: Path) -> None:
    """``reset_build_env`` is declared with ``**kwargs`` and ignores extras.

    Every ``firmware/*`` handler accepts ``**kwargs`` so the WS
    dispatcher's keyword-spread ``handler(client=…, message_id=…, **cmd.args)``
    doesn't choke when the frontend sends a bookkeeping field
    the handler doesn't read. Pin that contract — a refactor
    that tightens the signature would silently break those WS
    calls until they're individually retested.
    """
    controller = _controller(tmp_path)

    job = await controller.reset_build_env(client=object(), message_id="m1", spurious=True)

    assert job.job_type == JobType.RESET_BUILD_ENV


# ---------------------------------------------------------------------------
# _reset_build_env runner — the work the queued job actually performs
# ---------------------------------------------------------------------------


def _make_job() -> FirmwareJob:
    return FirmwareJob(
        job_id="abc123",
        configuration="",
        job_type=JobType.RESET_BUILD_ENV,
        status=JobStatus.RUNNING,
        output=[],
    )


def _seed_targets(config_dir: Path, *, names: tuple[str, ...] = _RESET_BUILD_ENV_TARGETS) -> None:
    """Lay out the ``.esphome/<target>/`` directories the runner expects.

    Each gets a sentinel file so an accidental ``rmtree`` of an
    *empty* dir would still register as removal of "real" content.
    """
    esphome_root = config_dir / ".esphome"
    for name in names:
        target = esphome_root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "sentinel").write_text("x", encoding="utf-8")


@pytest.mark.asyncio
async def test_reset_build_env_runner_removes_each_target(tmp_path: Path) -> None:
    """Every directory in ``_RESET_BUILD_ENV_TARGETS`` is removed.

    Uses the upstream constant so a future addition to the target
    list (e.g. a new cache directory) is automatically covered —
    the test passes only if each named directory is gone after
    the runner finishes.
    """
    controller = _controller(tmp_path)
    _seed_targets(tmp_path)
    job = _make_job()

    await controller._reset_build_env(job)

    for name in _RESET_BUILD_ENV_TARGETS:
        assert not (tmp_path / ".esphome" / name).exists(), (
            f"{name}/ survived reset_build_env — runner missed a target"
        )


@pytest.mark.asyncio
async def test_reset_build_env_runner_marks_job_completed(tmp_path: Path) -> None:
    """Successful completion sets ``COMPLETED`` + ``exit_code=0`` + ``progress=100``.

    The dashboard's job row uses ``status`` for the badge,
    ``exit_code`` for the success / failure decoration, and
    ``progress`` for the bar. All three need to land before the
    ``JOB_COMPLETED`` event so a frontend reading the post-event
    state sees a fully-finished job.
    """
    controller = _controller(tmp_path)
    _seed_targets(tmp_path)
    job = _make_job()

    await controller._reset_build_env(job)

    assert job.status == JobStatus.COMPLETED
    assert job.exit_code == 0
    assert job.progress == 100


@pytest.mark.asyncio
async def test_reset_build_env_runner_fires_job_completed(tmp_path: Path) -> None:
    """``JOB_COMPLETED`` fires with the finished job in its payload.

    Pairs with the run-followers panel: without this event the
    "Reset build environment" row would stick on RUNNING forever
    in the all-jobs view, even though the runner's local state
    flipped to COMPLETED.
    """
    controller = _controller(tmp_path)
    _seed_targets(tmp_path)
    job = _make_job()

    await controller._reset_build_env(job)

    controller._db.bus.fire.assert_any_call(EventType.JOB_COMPLETED, {"job": job})


@pytest.mark.asyncio
async def test_reset_build_env_runner_streams_output_lines(tmp_path: Path) -> None:
    """Progress lines hit both ``job.output`` and the bus.

    The follower-side ``follow_job`` panel renders lines from
    ``job.output`` for late-attaching clients (replay) and from
    ``JOB_OUTPUT`` events for live ones. Both surfaces need to
    see the same content; pin a representative line on each side
    so a refactor that drops one of the two writes surfaces here.
    """
    controller = _controller(tmp_path)
    _seed_targets(tmp_path)
    job = _make_job()

    await controller._reset_build_env(job)

    full_output = "".join(job.output)
    assert "Resetting build environment" in full_output
    assert "Reset complete" in full_output
    # Each removal target is named in a "removing X/" line.
    for name in _RESET_BUILD_ENV_TARGETS:
        assert f"removing {name}/" in full_output

    # Bus side: every line that landed in ``job.output`` was also
    # fired as a ``JOB_OUTPUT`` event so live followers see it.
    output_calls = [
        call
        for call in controller._db.bus.fire.call_args_list
        if call.args and call.args[0] == EventType.JOB_OUTPUT
    ]
    assert output_calls, "expected at least one JOB_OUTPUT broadcast"
    fired_lines = [call.args[1]["line"] for call in output_calls]
    assert any("Resetting build environment" in line for line in fired_lines)


@pytest.mark.asyncio
async def test_reset_build_env_runner_skips_missing_targets(tmp_path: Path) -> None:
    """A target that doesn't exist is logged as skipped, not fatal.

    Fresh installs don't have ``platformio_cache/`` until the
    first compile; the runner must succeed against a partially
    populated ``.esphome/``. Verify by seeding only one of the
    targets and confirming the runner completes (status COMPLETED)
    while still naming the missing ones in the output.
    """
    controller = _controller(tmp_path)
    # Only seed the build dir; external_components and platformio_cache absent.
    _seed_targets(tmp_path, names=("build",))
    job = _make_job()

    await controller._reset_build_env(job)

    assert job.status == JobStatus.COMPLETED
    full_output = "".join(job.output)
    for name in _RESET_BUILD_ENV_TARGETS:
        if name == "build":
            assert f"removing {name}/" in full_output
        else:
            assert f"skipped (not present): {name}/" in full_output


@pytest.mark.asyncio
async def test_reset_build_env_runner_no_op_when_esphome_absent(tmp_path: Path) -> None:
    """``.esphome/`` not yet created → "Nothing to do" + COMPLETED.

    Edge case for never-compiled config dirs: the runner shouldn't
    create the directory just to wipe it, and shouldn't fail
    because the targets don't exist. Pins the early-exit branch
    that checks ``esphome_root.exists()``.
    """
    controller = _controller(tmp_path)
    # Don't call _seed_targets — leave .esphome absent entirely.
    job = _make_job()

    await controller._reset_build_env(job)

    assert job.status == JobStatus.COMPLETED
    assert "Nothing to do" in "".join(job.output)
    # Neither created nor wiped: directory still doesn't exist.
    assert not (tmp_path / ".esphome").exists()


@pytest.mark.asyncio
async def test_reset_build_env_runner_honours_cancel_between_targets(tmp_path: Path) -> None:
    """Cancellation requested mid-run stops before the next target.

    ``shutil.rmtree`` isn't interruptible from another coroutine,
    so the user-visible promise is "the runner stops at the next
    target boundary". Seed all targets, mark the job as cancelled
    *before* the runner starts, and assert:

    - The runner returns without removing any target (cancel
      check fires before each one).
    - Status is CANCELLED, not COMPLETED.
    - ``JOB_CANCELLED`` fires for the all-jobs panel.
    - The cancel id is consumed (popped from
      ``self._cancel_requested``) so a re-queued job with the same
      id wouldn't auto-cancel.
    """
    controller = _controller(tmp_path)
    _seed_targets(tmp_path)
    job = _make_job()
    controller._cancel_requested.add(job.job_id)

    await controller._reset_build_env(job)

    assert job.status == JobStatus.CANCELLED
    controller._db.bus.fire.assert_any_call(EventType.JOB_CANCELLED, {"job": job})
    assert job.job_id not in controller._cancel_requested
    # All targets still present — runner bailed before the first rmtree.
    for name in _RESET_BUILD_ENV_TARGETS:
        assert (tmp_path / ".esphome" / name / "sentinel").exists()
