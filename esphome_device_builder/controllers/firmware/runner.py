"""Firmware-job runner: queue loop + local subprocess execution + remote dispatch."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...helpers.subprocess import create_subprocess_exec, iter_lines_with_progress
from ...models import (
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobSource,
    JobStatus,
    JobType,
)
from .constants import _ERROR_PATTERNS
from .helpers import (
    _ingest_output_line,
    _is_no_module_named_esphome,
    _trim_job_output,
)
from .remote_runner import run_remote_job

if TYPE_CHECKING:
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)


async def run_queue(controller: FirmwareController) -> None:
    """Background loop: process one job at a time."""
    while True:
        job = await controller.state.queue.get()
        if job.status == JobStatus.CANCELLED:
            continue
        await controller._execute_job(job)


async def execute_job(  # noqa: PLR0912, PLR0915
    controller: FirmwareController, job: FirmwareJob
) -> None:
    """Execute a single firmware job."""
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(UTC).isoformat()
    controller.state.current_job = job
    _LOGGER.info(
        "Starting job %s: %s %s",
        job.job_id,
        job.job_type,
        job.configuration,
    )
    started_payload: JobLifecycleData = {"job": job}
    controller._db.bus.fire(EventType.JOB_STARTED, started_payload)
    await controller._persist_jobs()

    try:
        # Source-routed branch: REMOTE-source jobs dispatch via
        # peer-link to a paired receiver instead of running a
        # local subprocess. The receiver's ``OFFLOADER_JOB_*``
        # fan-out events drive the same lifecycle / output /
        # progress fires every local subscriber already
        # consumes — follow_job and the firmware-tasks UI don't
        # need to know whether the bytes are local or remote.
        if job.source is JobSource.REMOTE:
            await controller._execute_remote_job(job)
            return

        # Pre-flight: verify chip type for serial uploads
        if job.job_type in (JobType.UPLOAD, JobType.INSTALL):
            await controller._verify_chip(job)

        # ``rel_path`` calls ``Path.resolve`` which does a sync
        # ``os.path.realpath`` — blocking the event loop. Push it
        # to the executor so the runner stays non-blocking
        # end-to-end (matters even for the runner because
        # ``bus.fire`` listeners are interleaved on the loop and
        # blocking here pauses every follower's event delivery).
        loop = asyncio.get_running_loop()
        config_path = str(
            await loop.run_in_executor(None, controller._db.settings.rel_path, job.configuration)
        )
        cache_args = controller._build_cache_args(job)
        cmd = controller._build_command(
            job.job_type, config_path, job.port, cache_args, job.new_name
        )
        _LOGGER.debug("Running: %s", " ".join(cmd))

        env = controller._compose_subprocess_env(job)
        has_error_in_output = False
        # Captured at append time because the in-flight trim can
        # elide the offending line before the post-exit handler
        # runs. ``_check_error`` already had the line in hand
        # there; persisting the verdict here lets the post-exit
        # handler render a specific actionable message even
        # after a long noisy build trims the head.
        saw_no_esphome_module = False

        def _check_error(text: str) -> None:
            nonlocal has_error_in_output, saw_no_esphome_module
            if not saw_no_esphome_module and _is_no_module_named_esphome(text):
                saw_no_esphome_module = True
            if has_error_in_output:
                return
            for pattern in _ERROR_PATTERNS:
                if pattern in text:
                    has_error_in_output = True
                    return

        async with controller._tracked_subprocess(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            # Put the whole esphome → platformio → gcc tree in its
            # own process group so ``_terminate_current_process``
            # can signal the entire chain, not just the python
            # parent. Without this, killing the parent leaves the
            # compiler children orphaned and the build keeps
            # running until they finish on their own — exactly the
            # "stop compile doesn't work" symptom.
            start_new_session=True,
        ) as proc:
            # Honour a cancel that landed in the gap between
            # ``_verify_chip`` finishing and ``create_subprocess_exec``
            # returning — without this, an early Stop click during
            # the brief async window where ``_current_process`` was
            # ``None`` lets the install run to completion before the
            # post-``proc.wait()`` cancel check sees the flag.
            if job.job_id in controller.state.cancel_requested:
                await controller._terminate_current_process()

            assert proc.stdout is not None  # type narrowing

            # ``iter_lines_with_progress`` splits on `\n` _or_ `\r`
            # so carriage-return-based in-place updates (esptool's
            # `Writing at 0x... (5%)\r`, PlatformIO's progress
            # bars) survive the pipe instead of getting buffered
            # until the next newline. Each chunk keeps its
            # trailing terminator so the frontend can decide
            # whether to append a new line or overwrite the last
            # one.
            async for line in iter_lines_with_progress(proc.stdout):
                # Shared with the source-routed remote runner
                # (``remote_runner._on_output``). The helper
                # buffers + trims + fires ``JOB_OUTPUT`` and
                # advances ``JOB_PROGRESS`` on a parseable
                # percentage — same per-line bookkeeping
                # whether the build's bytes come from this
                # CPU or a paired receiver. ``_check_error``
                # stays inline because it mutates the
                # nonlocal ``has_error_in_output`` /
                # ``saw_no_esphome_module`` flags the
                # post-exit handler reads; remote builds
                # surface a structured ``failed`` status from
                # the receiver instead, so the stderr scrape
                # only matters here.
                _ingest_output_line(job, controller._db.bus, line)
                _check_error(line)

            exit_code = await proc.wait()
            job.exit_code = exit_code

        # If the user cancelled this job mid-run, the subprocess
        # exits non-zero (terminated by signal). Honour that
        # intent rather than reporting it as a generic failure.
        if job.job_id in controller.state.cancel_requested:
            controller._finalize_cancelled(job)
            _LOGGER.info("Job %s cancelled mid-run (exit %s)", job.job_id, exit_code)
        else:
            success = exit_code == 0 and not has_error_in_output
            if has_error_in_output and exit_code == 0:
                if saw_no_esphome_module:
                    job.error = (
                        "esphome is not importable from the dashboard's Python "
                        f"environment ({sys.executable}). Install it with "
                        "``pip install -e '.[esphome]'`` "
                        "(or ``pip install esphome``) "
                        "in the same venv and restart the dashboard."
                    )
                else:
                    job.error = "Process exited 0 but output contains errors"
                _LOGGER.warning("Job %s: %s", job.job_id, job.error)

            # ``_finalize_terminal`` runs the mark + slot-
            # release + fire sequence in the order the
            # ``queue_status`` broadcaster needs (see helper
            # docstring for the regression context).
            controller._finalize_terminal(job, JobStatus.COMPLETED if success else JobStatus.FAILED)
            _LOGGER.info(
                "Job %s %s (exit code %s)",
                job.job_id,
                job.status,
                exit_code,
            )

    except asyncio.CancelledError:
        # ``_tracked_subprocess`` already terminated the spawn
        # on its way out; this branch only needs to finalise
        # the job model and fire the event.
        controller._finalize_cancelled(job)
        _LOGGER.info("Job %s cancelled (runner shutdown)", job.job_id)
        raise
    except Exception as exc:
        # If a cancel was requested before this exception escaped,
        # honour it as CANCELLED instead of FAILED. The
        # ``_verify_chip`` early-cancel path raises ``ValueError``
        # to short-circuit the install — without this branch
        # that error would be reported as a generic failure
        # rather than the user-driven cancel it actually is.
        if job.job_id in controller.state.cancel_requested:
            controller._finalize_cancelled(job)
            _LOGGER.info("Job %s cancelled before subprocess wait: %s", job.job_id, exc)
        else:
            job.error = str(exc)
            controller._finalize_terminal(job, JobStatus.FAILED)
            _LOGGER.exception("Job %s failed", job.job_id)
    finally:
        controller.state.current_job = None
        controller.state.current_process = None
        if job.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            _trim_job_output(job)
            controller._prune_history()
        await controller._persist_jobs()


async def execute_remote_job(controller: FirmwareController, job: FirmwareJob) -> None:
    """
    Run a ``JobSource.REMOTE`` job by dispatching through peer-link.

    Reads ``source_pin_sha256`` off *job*, looks up the live
    :class:`PeerLinkClient` through the remote-build
    controller, bundles the YAML via the ``esphome bundle``
    subprocess, dispatches ``submit_job(target="compile")``,
    then translates receiver-side ``OFFLOADER_JOB_OUTPUT`` /
    ``OFFLOADER_JOB_STATE_CHANGED`` events into the same
    local ``JOB_OUTPUT`` / ``JOB_PROGRESS`` /
    ``JOB_<terminal>`` fires the local subprocess path emits.
    ``follow_job`` and the firmware-tasks UI consume one
    event stream regardless of which CPU compiled the bytes.

    Dispatches by ``job.job_type``:

    * :attr:`JobType.COMPILE` — wait for the receiver's
      terminal frame, finalise based on the wire status.
    * :attr:`JobType.UPLOAD` / :attr:`JobType.INSTALL` —
      same compile dispatch (per § Transparent install
      flow's load-bearing "receiver only ever compiles"
      policy), but on receiver-completed pull the
      artifacts back via ``download_artifacts`` and run a
      local ``esphome upload --file <staged>`` subprocess
      to flash the device. The local flash step shares the
      ``_tracked_subprocess`` plumbing the LOCAL path uses
      so cancel SIGTERM lands on the upload chain the same
      way.

    Other job types (``CLEAN`` / ``RENAME`` /
    ``RESET_BUILD_ENV``) are rejected at the runner's top
    because the receiver-side ``submit_job`` contract is
    compile-only — these don't have a corresponding wire
    flow.

    Terminal states are mapped through the same helpers the
    local path uses (``_mark_job_terminal`` /
    ``_finalize_cancelled``), so the outer
    ``_execute_job``'s ``finally`` runs the shared
    ``_trim_job_output`` / ``_prune_history`` / persist
    sequence regardless of which branch produced the
    terminal status.
    """
    await run_remote_job(controller, job)


@asynccontextmanager
async def tracked_subprocess(
    controller: FirmwareController, *args: Any, **kwargs: Any
) -> AsyncIterator[asyncio.subprocess.Process]:
    """
    Spawn a subprocess that's visible to ``firmware/cancel``.

    Required for every ``create_subprocess_exec`` call in the
    runner path — both the main install/upload spawn in
    ``_execute_job`` and pre-flight probes like
    ``_verify_chip``. Setting ``_current_process`` is what lets
    a concurrent ``firmware/cancel`` actually land SIGTERM on
    the running spawn; a direct ``create_subprocess_exec`` call
    without this registration silently regresses the
    issue-#136 fix — the cancel handler walks
    ``_current_process``, no-ops on ``None``, the user clicks
    Stop, nothing visible happens, and the orphaned subprocess
    runs to completion in the background.

    Two cleanup contracts on exit:

    - Normal exit / non-cancellation exception: restore the
      prior ``_current_process`` value so nested usage (a
      future spawn site that itself wraps another) doesn't
      accidentally null out an outer registration.
    - ``asyncio.CancelledError`` (runner-task shutdown):
      terminate the spawn before propagating, so the build
      can't outlive the runner that started it. The outer
      ``except asyncio.CancelledError`` in ``_execute_job``
      handles the job-finalisation half and relies on this
      helper for the terminate.

    Pairs with ``_raise_if_cancelled`` — wrap each spawn, then
    call the helper after to short-circuit if the cancel landed
    between this subprocess and the next one.
    """
    proc = await create_subprocess_exec(*args, **kwargs)
    prev = controller.state.current_process
    controller.state.current_process = proc
    try:
        yield proc
    except asyncio.CancelledError:
        # Runner-shutdown cancellation: the runner task itself
        # was cancelled (vs. a user-driven ``firmware/cancel``,
        # which calls ``_terminate_current_process`` from the
        # cancel handler directly). Reuse the same group-aware
        # termination helper here so SIGTERM walks the whole
        # process group (esphome → platformio → gcc / esptool).
        # ``proc.terminate()`` would only signal the python
        # parent — on POSIX with ``start_new_session=True``
        # that orphans the child tree and the build keeps
        # running until the children finish on their own.
        await controller._terminate_current_process()
        raise
    finally:
        controller.state.current_process = prev
