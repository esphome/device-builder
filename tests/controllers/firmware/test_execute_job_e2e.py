"""End-to-end coverage for ``FirmwareController._execute_job``.

Drives the runner through the public submission API
(``firmware/compile``) rather than calling ``_execute_job``
directly so the test exercises the full path the runner walks
in production:

    enqueue (``compile``) → ``_run_queue`` pops → ``_execute_job``
    → subprocess spawn → stdout streamed line by line → bus.fire
    → exit code + error-pattern verdict → JOB_COMPLETED /
    JOB_FAILED / JOB_CANCELLED broadcast → finally trim + persist

The "subprocess" is a Python one-liner pointed to by
``_esphome_cmd``; each test parametrises the script body to
exercise a different branch of the runner (success, exit-code
failure, exit-0 + error-pattern, mid-stream cancel, progress
parsing, ``No module named 'esphome'`` actionable hint).

Without this file most of ``_execute_job`` (~180 lines, by far
the biggest method in ``FirmwareController``) was uncovered —
every other test in this directory either stubbed the runner
out or exercised a single helper in isolation. A regression in
the spawn / stream / exit-handling chain would silently break
every dashboard build with no test failure.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import EventType, JobStatus

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


# ---------------------------------------------------------------------------
# Fixture: a real runner task driving a real ``asyncio.Queue``
# ---------------------------------------------------------------------------


def _wire_real_queue(controller: FirmwareController) -> None:
    """Swap the conftest's ``AsyncMock`` queue for a real ``asyncio.Queue``.

    The runner does ``await self._queue.get()``; an ``AsyncMock``
    returns its default sentinel immediately and the runner would
    spin instead of waiting for a real submission. Pair the queue
    swap with the supersede stub (passthrough) and the
    cancel-tracking surface ``_execute_job`` reads.
    """
    controller._queue = asyncio.Queue()

    async def _supersede(_configuration: str, *, exclude_job_id: str) -> None:
        return

    controller._supersede_active_jobs = _supersede  # type: ignore[assignment]
    controller._current_job = None
    controller._current_process = None
    controller._cancel_requested = set()


def _fake_esphome(controller: FirmwareController, script: str) -> None:
    """Point ``_esphome_cmd`` at an inline Python script.

    ``_build_command`` produces ``[*self._esphome_cmd, '--dashboard',
    *cache_args, '<subcommand>', '<config_path>', ...]`` — so the
    script will see ``sys.argv == [<script>, '--dashboard', 'compile',
    '<path>']``. Scripts ignore the args and just emit the output
    shape the test wants to exercise.
    """
    controller._esphome_cmd = [sys.executable, "-c", script]


def _seed_yaml(tmp_path: Path, name: str = "kitchen.yaml") -> None:
    (tmp_path / name).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")


async def _run_until_terminal(
    controller: FirmwareController, *, timeout: float = 10.0
) -> dict[str, list]:
    """Run the queue runner until the next terminal event fires.

    Subscribes to JOB_STARTED / JOB_OUTPUT / JOB_PROGRESS /
    JOB_COMPLETED / JOB_FAILED / JOB_CANCELLED, returns the
    captured records keyed by event-type value. Works for any
    test that submits exactly one job — terminal events are
    one-per-job so the first one delivered ends the wait.

    Falls back to a hard timeout so a runner regression that
    never delivers a terminal event surfaces as a clean test
    failure rather than a hung pytest run.
    """
    captured: dict[str, list] = {
        "job_started": [],
        "job_output": [],
        "job_progress": [],
        "job_completed": [],
        "job_failed": [],
        "job_cancelled": [],
    }
    terminal_event = asyncio.Event()

    bus = controller._db.bus
    real_fire = bus.fire

    def _capture(event_type: EventType, data: dict) -> None:
        key = event_type.value
        if key in captured:
            captured[key].append(data)
        if key in ("job_completed", "job_failed", "job_cancelled"):
            terminal_event.set()
        # Forward to the original mock so call-count assertions still work.
        real_fire(event_type, data)

    bus.fire = _capture

    runner_task = asyncio.create_task(controller._run_queue())
    try:
        await asyncio.wait_for(terminal_event.wait(), timeout=timeout)
    finally:
        runner_task.cancel()
        with suppress(asyncio.CancelledError):
            await runner_task

    return captured


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_runs_subprocess_to_completion(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """Submit → runner pops → subprocess runs → COMPLETED.

    The full pipeline: ``compile`` enqueues, ``_run_queue`` pops,
    ``_execute_job`` builds the command and spawns the subprocess,
    streams stdout into ``job.output``, fires ``JOB_OUTPUT`` for
    each line, and on exit_code 0 marks the job ``COMPLETED`` and
    fires ``JOB_COMPLETED``. Verify each of those side-effects
    landed without inspecting any internal helper directly.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        # Two-line stdout, exit 0. Each line lands in job.output and
        # in a JOB_OUTPUT broadcast.
        "import sys\n"
        "print('INFO Reading configuration kitchen.yaml...')\n"
        "print('INFO Compile finished.')\n"
        "sys.exit(0)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")

    captured = await _run_until_terminal(controller)

    assert job.status == JobStatus.COMPLETED
    assert job.exit_code == 0
    assert captured["job_started"]
    assert captured["job_started"][0]["job"].job_id == job.job_id
    # Both stdout lines reach the live stream.
    output_lines = [d["line"] for d in captured["job_output"]]
    assert any("Reading configuration" in line for line in output_lines)
    assert any("Compile finished" in line for line in output_lines)
    # And the same lines are buffered on the job for late-attaching
    # followers to replay.
    assert "".join(job.output).count("Reading configuration") == 1
    # Single terminal broadcast, not failed/cancelled.
    assert len(captured["job_completed"]) == 1
    assert captured["job_failed"] == []
    assert captured["job_cancelled"] == []


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_nonzero_exit_marks_failed(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """Subprocess exits non-zero → status FAILED, JOB_FAILED fires.

    The "build broke" path. Without this branch the runner would
    silently mark every job COMPLETED regardless of compiler
    errors and the dashboard's red-vs-green status badge would
    be useless.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        "import sys\nprint('compile error: undefined reference')\nsys.exit(7)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")
    captured = await _run_until_terminal(controller)

    assert job.status == JobStatus.FAILED
    assert job.exit_code == 7
    assert captured["job_failed"]
    assert captured["job_failed"][0]["job"] is job
    assert captured["job_completed"] == []


@pytest.mark.asyncio
async def test_compile_exit_zero_with_error_pattern_marks_failed(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """Exit 0 + error pattern in output → still FAILED.

    Some failure modes exit 0 but print a Python traceback through
    ``print()`` (e.g. an external_components script that swallows
    the exit code). The runner pattern-matches each line against
    ``_ERROR_PATTERNS`` so those don't render as green builds.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        "import sys\nprint('Traceback (most recent call last):')\n"
        "print(\"ModuleNotFoundError: No module named 'cryptography'\")\n"
        "sys.exit(0)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")
    captured = await _run_until_terminal(controller)

    assert job.status == JobStatus.FAILED
    assert job.exit_code == 0
    assert job.error and "exit" in job.error.lower()
    assert captured["job_failed"]
    assert captured["job_completed"] == []


@pytest.mark.asyncio
async def test_compile_no_module_named_esphome_renders_actionable_hint(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """``No module named 'esphome'`` produces the install-hint message.

    The most common deployment failure (esphome not installed in
    the dashboard's venv) needs a specific actionable message
    pointing at ``pip install -e '.[esphome]'`` rather than the
    generic "Process exited 0 but output contains errors".

    Captured at append time (``saw_no_esphome_module``) so the
    in-flight trim can't elide the offending line before the
    post-exit handler renders the hint. The exact CPython quoted
    form (``No module named 'esphome'``) avoids false-positive
    sibling matches like ``esphome_dashboard``.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        "import sys\nprint('Traceback (most recent call last):')\n"
        # Single-quoted module name — the exact form CPython emits.
        "print(\"ModuleNotFoundError: No module named 'esphome'\")\n"
        "sys.exit(0)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")
    await _run_until_terminal(controller)

    assert job.status == JobStatus.FAILED
    assert job.error is not None
    assert "esphome is not importable" in job.error
    assert "pip install" in job.error


# ---------------------------------------------------------------------------
# Mid-run cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_mid_run_cancel_marks_cancelled(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """Cancel requested mid-run → status CANCELLED, not FAILED.

    The user-cancelled path: the runner subprocess gets terminated
    (or completes with a non-zero exit because we sent SIGTERM),
    and the runner consults ``self._cancel_requested`` to
    distinguish "user pulled the plug" from "the build genuinely
    failed". Without this branch every cancel would render as a
    red FAILED row in the dashboard's job table, confusing the
    user about whether their cancel was respected.

    Sequencing matters here:

    - JOB_STARTED fires *before* the subprocess spawn (the runner
      flips the status before it ``await``s ``create_subprocess_exec``).
      Synchronising on it would race the spawn and we'd terminate
      a process that hasn't been assigned to ``_current_process``
      yet. So we wait for the first JOB_OUTPUT instead — that's
      the earliest signal the subprocess is alive AND the
      ``_current_process`` attribute has been written.
    - We must wait for JOB_CANCELLED to fire *before* cancelling
      the runner task. Otherwise ``runner_task.cancel()`` triggers
      ``_execute_job``'s own ``except asyncio.CancelledError``
      branch (which also fires JOB_CANCELLED + marks the job
      CANCELLED), and the assertions below would pass even if the
      genuine post-``proc.wait()`` user-cancel branch we're
      supposed to be testing was broken.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        # Block forever until the parent kills us. ``stdin.read``
        # waits on EOF; closing or terminating the pipe ends it.
        "import sys\nprint('starting...', flush=True)\nsys.stdin.read()\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")

    proc_alive = asyncio.Event()
    cancelled_fired = asyncio.Event()
    captured: list[dict] = []
    real_fire = controller._db.bus.fire

    def _watch(event_type: EventType, data: dict) -> None:
        captured.append({"type": event_type, "data": data})
        # First JOB_OUTPUT line means the subprocess is up,
        # streaming through ``iter_lines_with_progress``, and
        # ``self._current_process`` has been assigned.
        if event_type == EventType.JOB_OUTPUT:
            proc_alive.set()
        elif event_type == EventType.JOB_CANCELLED:
            cancelled_fired.set()
        real_fire(event_type, data)

    controller._db.bus.fire = _watch

    runner_task = asyncio.create_task(controller._run_queue())
    try:
        await asyncio.wait_for(proc_alive.wait(), timeout=10.0)
        # The subprocess is now blocking in ``sys.stdin.read()``.
        # Mark cancel + terminate the process — the runner picks
        # up the cancel flag when it loops back to read the next
        # line and sees EOF.
        controller._cancel_requested.add(job.job_id)
        assert controller._current_process is not None
        controller._current_process.terminate()

        # Wait for the cancel event from the runner's natural
        # post-``proc.wait()`` path, NOT from ``runner_task.cancel()``
        # below. If we cancelled the task here without waiting,
        # ``_execute_job``'s ``except CancelledError`` branch would
        # fire JOB_CANCELLED too and the test couldn't distinguish
        # "user-cancel path worked" from "task-cancel path worked".
        await asyncio.wait_for(cancelled_fired.wait(), timeout=10.0)
    finally:
        runner_task.cancel()
        with suppress(asyncio.CancelledError):
            await runner_task

    assert job.status == JobStatus.CANCELLED
    # Subprocess actually exited (the user-cancel branch awaits
    # ``proc.wait()`` before deciding the verdict). A regression
    # that bailed on the cancel-flag check before the await would
    # leave ``exit_code`` as ``None``.
    assert job.exit_code is not None
    assert any(c["type"] == EventType.JOB_CANCELLED for c in captured)
    assert not any(c["type"] == EventType.JOB_FAILED for c in captured)
    # Cancel id is consumed so a re-queue with the same id wouldn't auto-cancel.
    assert job.job_id not in controller._cancel_requested


# ---------------------------------------------------------------------------
# Progress parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_progress_lines_fire_job_progress(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """A monotonically-increasing percentage emits ``JOB_PROGRESS`` events.

    Progress reporting is what drives the dashboard's per-job
    progress bar. Each ``[NN%]``-shaped PlatformIO line should
    surface as a JOB_PROGRESS broadcast, monotonically clamped
    so a later "0%" from the next phase doesn't visually rewind
    the bar.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        "import sys\n"
        "print('[ 25%] Compiling foo.cpp.o')\n"
        "print('[ 50%] Compiling bar.cpp.o')\n"
        "print('[100%] Built kitchen.elf')\n"
        "sys.exit(0)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")
    captured = await _run_until_terminal(controller)

    progress_values = [d["progress"] for d in captured["job_progress"]]
    # 25 → 50 → 100, monotonically non-decreasing.
    assert progress_values == [25, 50, 100]
    # Final job state reflects the highest reading, not whatever
    # arrived last (a regression to "last write wins" would let
    # a 0% line clobber the bar).
    assert job.progress == 100
    assert job.status == JobStatus.COMPLETED
