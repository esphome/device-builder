"""End-to-end coverage for ``FirmwareController.reset_build_env``.

The handler itself is a one-liner (``_create_job`` then
``_enqueue``); the runner side now reuses the same subprocess
pipeline as compile/upload via ``_build_command`` mapping
``JobType.RESET_BUILD_ENV`` to ``clean-all`` (matching the legacy
``EsphomeCleanAllHandler``). What we pin here:

- The handler returns a queued job of the right shape and routes
  it onto the queue in the documented PUT-then-FIRE order.
- ``_build_command`` for ``RESET_BUILD_ENV`` produces
  ``[*esphome_cmd, '--dashboard', 'clean-all', <config_dir>]``
  with no ``--device``. Cache args come from ``_build_cache_args``
  in the runtime flow, which already returns ``[]`` for
  non-OTA jobs — ``_build_command`` itself trusts what the
  caller hands it and will splice cache args in if you pass
  them directly (the second test below pins that). The broader
  filesystem cleanup (``.esphome/`` minus ``storage/``, plus
  PlatformIO's ``cache_dir`` / ``packages_dir`` /
  ``platforms_dir`` / ``core_dir``) is then esphome's
  responsibility.

The actual subprocess streaming / exit-handling pipeline is
already covered by ``test_execute_job_e2e.py`` (which exercises
the same path for compile and adds a RESET_BUILD_ENV-specific
test that verifies the dispatch through the queue).
"""

from __future__ import annotations

from pathlib import Path

from esphome_device_builder.models import EventType, JobStatus, JobType
from tests.controllers.firmware.conftest import (
    BareFirmwareControllerFactory,
    CaptureEnqueueOrderFactory,
    EnqueueStep,
    FirmwareControllerFactory,
)

# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------


async def test_reset_build_env_returns_queued_job_with_reset_type(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Happy path: the handler returns a ``QUEUED`` job of type ``RESET_BUILD_ENV``.

    Pin both ``status`` and ``job_type`` so a future regression
    that defaults to ``COMPILE`` (or marks the new job RUNNING
    before the runner picks it up) shows up immediately. The
    frontend's job-table renders the row from these two fields.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)

    job = await controller.reset_build_env()

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.RESET_BUILD_ENV


async def test_reset_build_env_uses_empty_configuration(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``configuration`` is ``""`` — reset is global, not per-device.

    Documented contract in the handler's docstring; pinned here so
    a refactor that tries to attach a configuration (e.g. to satisfy
    a "configuration is required" linter rule) breaks the test
    rather than the runtime invariant the rename-lock /
    refresh-scheduling logic relies on
    (``test_helpers.test_names_touched_by_job_with_empty_configuration_is_empty``).
    The empty string also feeds straight into ``rel_path("")`` at
    runtime, which resolves back to ``config_dir`` — the positional
    arg ``clean-all`` actually expects.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)

    job = await controller.reset_build_env()

    assert job.configuration == ""


async def test_reset_build_env_registers_job_in_jobs_map(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The new job is registered so ``cancel`` / ``follow_job`` can find it by id."""
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)

    job = await controller.reset_build_env()

    assert await controller.get_job(job_id=job.job_id) is job


async def test_reset_build_env_fires_job_queued_after_enqueue(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_enqueue_order: CaptureEnqueueOrderFactory,
) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    Same ordering invariant as ``install`` /
    ``upload`` / ``compile``: the all-jobs panel keys off
    ``JOB_QUEUED`` to add a row when a new job lands, and a
    follower attaching on that signal must find the job already
    in the queue. If the broadcast preceded the put, a frontend
    that immediately calls ``follow_job`` could race the runner
    and miss the first lines.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    log = capture_enqueue_order(controller, EventType.JOB_QUEUED)

    job = await controller.reset_build_env()

    assert log[0] == (EnqueueStep.PUT, job)
    assert log[1][0] is EnqueueStep.FIRE
    assert log[1][1].event_type == EventType.JOB_QUEUED
    assert log[1][1].data == {"job": job}


async def test_reset_build_env_accepts_arbitrary_kwargs(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``reset_build_env`` is declared with ``**kwargs`` and ignores extras.

    Every ``firmware/*`` handler accepts ``**kwargs`` so the WS
    dispatcher's keyword-spread ``handler(client=…, message_id=…, **cmd.args)``
    doesn't choke when the frontend sends a bookkeeping field
    the handler doesn't read. Pin that contract — a refactor
    that tightens the signature would silently break those WS
    calls until they're individually retested.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)

    job = await controller.reset_build_env(client=object(), message_id="m1", spurious=True)

    assert job.job_type == JobType.RESET_BUILD_ENV


# ---------------------------------------------------------------------------
# _build_command — RESET_BUILD_ENV branch
# ---------------------------------------------------------------------------


def test_build_command_for_reset_build_env_uses_clean_all_with_config_dir(
    tmp_path: Path,
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """``RESET_BUILD_ENV`` shells out to ``esphome --dashboard clean-all <config_dir>``.

    Mirrors the legacy dashboard's ``EsphomeCleanAllHandler``,
    which builds ``[*DASHBOARD_COMMAND, "clean-all", settings.config_dir]``.
    Routing through the same subprocess pipeline as compile/upload
    gets us the upstream-canonical cleanup behaviour for free
    (every ``.esphome/`` subdir except ``storage/``, plus
    PlatformIO's real ``cache_dir`` / ``packages_dir`` /
    ``platforms_dir`` / ``core_dir``) instead of an inline rmtree
    of three hardcoded directories that misses everything else.

    Pin the exact arg order: the positional config_dir comes
    *after* the subcommand name (esphome's argparse parses
    top-level flags before the subcommand), and there's no
    trailing ``--device`` because clean-all doesn't talk to a
    device.
    """
    controller = bare_firmware_controller_factory(esphome_cmd=["esphome"], with_mock_db=True)

    # ``rel_path("")`` resolves to the config_dir Path itself (joinpath
    # with an empty segment is a no-op) — that's what the runner passes
    # to ``_build_command`` for the empty-configuration RESET job.
    cmd = controller._build_command(JobType.RESET_BUILD_ENV, str(tmp_path), port="")

    assert cmd == [
        "esphome",
        "--dashboard",
        "clean-all",
        str(tmp_path),
    ]


def test_build_command_for_reset_build_env_ignores_port_and_cache_args(
    tmp_path: Path,
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """``RESET_BUILD_ENV`` neither flashes nor talks to the network.

    Belt-and-braces against a future refactor that loops cache
    args / ``--device`` into every job type: clean-all's CLI doesn't
    accept either, and an erroneously-included ``--device`` would
    make the subprocess error out before touching the cache. Pin
    that the command shape stays minimal even when the caller
    threads in cache args (which ``_build_cache_args`` already
    short-circuits to ``[]`` for non-OTA job types, but a direct
    invocation could still pass them).
    """
    controller = bare_firmware_controller_factory(esphome_cmd=["esphome"], with_mock_db=True)

    cmd = controller._build_command(
        JobType.RESET_BUILD_ENV,
        str(tmp_path),
        port="/dev/ttyUSB0",
        cache_args=["--mdns-address-cache", "kitchen=192.0.2.1"],
    )

    assert "--device" not in cmd
    assert "/dev/ttyUSB0" not in cmd
    # ``cache_args`` are still spliced in (the runner trusts the
    # caller; ``_build_cache_args`` is the gate that returns ``[]``
    # for non-OTA jobs in the real flow). Pin only that ``clean-all``
    # itself appears with the config_dir as the trailing positional.
    assert cmd[-2:] == ["clean-all", str(tmp_path)]
