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
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.firmware import (
    helpers as helpers_module,
)
from esphome_device_builder.controllers.firmware import (
    lifecycle as lifecycle_module,
)
from esphome_device_builder.controllers.firmware.constants import (
    _INFLIGHT_TRIM_KEEP,
    _MAX_OUTPUT_LINES_INFLIGHT,
)
from esphome_device_builder.models import EventType, JobStatus
from tests.controllers.firmware.conftest import (
    run_until_terminal as _run_until_terminal,
)
from tests.controllers.firmware.conftest import (
    wire_real_queue as _wire_real_queue,
)

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


# ---------------------------------------------------------------------------
# Fixture: a real runner task driving a real ``asyncio.Queue``
# ---------------------------------------------------------------------------


def _fake_esphome(controller: FirmwareController, script: str) -> None:
    """Point ``_esphome_cmd`` at an inline Python script.

    ``_build_command`` produces ``[*self.state.esphome_cmd, '--dashboard',
    *cache_args, '<subcommand>', '<config_path>', ...]`` — so the
    script will see ``sys.argv == [<script>, '--dashboard', 'compile',
    '<path>']``. Scripts ignore the args and just emit the output
    shape the test wants to exercise.
    """
    controller.state.esphome_cmd = [sys.executable, "-c", script]


def _seed_yaml(tmp_path: Path, name: str = "kitchen.yaml") -> None:
    (tmp_path / name).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


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


async def test_compile_platformio_no_module_named_pip_shrug_is_not_failure(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """PlatformIO ``python: No module named pip`` lines on an exit-0 build stay COMPLETED."""
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        "import sys\n"
        "print('[nanopb] Installing Protocol Buffers dependencies')\n"
        "print('/root/.platformio/penv/bin/python: No module named pip')\n"
        "print('[nanopb] Installing gRPC dependencies')\n"
        "print('/root/.platformio/penv/bin/python: No module named pip')\n"
        "print('[nanopb] No generation needed.')\n"
        "print('=========== [SUCCESS] Took 260.63 seconds ===========')\n"
        "print('INFO Successfully compiled program.')\n"
        "print('INFO OTA successful')\n"
        "print('INFO Successfully uploaded program.')\n"
        "sys.exit(0)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")
    captured = await _run_until_terminal(controller)

    assert job.status == JobStatus.COMPLETED
    assert job.exit_code == 0
    assert job.error is None
    assert captured["job_completed"]
    assert captured["job_failed"] == []


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


async def test_compile_mid_run_cancel_marks_cancelled(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """Cancel requested mid-run → status CANCELLED, not FAILED.

    The user-cancelled path: the runner subprocess gets terminated
    (or completes with a non-zero exit because we sent SIGTERM),
    and the runner consults ``self.state.cancel_requested`` to
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
        # ``self.state.compile_lane.current_process`` has been assigned.
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
        controller.state.cancel_requested.add(job.job_id)
        assert controller.state.compile_lane.current_process is not None
        controller.state.compile_lane.current_process.terminate()

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
    assert job.job_id not in controller.state.cancel_requested


async def test_execute_job_runner_shutdown_terminates_and_marks_cancelled(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """Cancelling the runner task mid-run hits the ``CancelledError`` branch.

    Distinct from the user-cancel path above: here nothing populates
    ``_cancel_requested``. The runner is awaiting on the subprocess's
    stdout when the task is cancelled (e.g. dashboard shutdown), so
    ``_execute_job``'s ``except asyncio.CancelledError`` is what fires
    JOB_CANCELLED, terminates the live process, and re-raises so the
    surrounding runner loop unwinds.

    Sequencing: wait for the first JOB_OUTPUT (proves the subprocess
    is up *and* ``_current_process`` has been assigned) before
    cancelling, otherwise we'd race the subprocess spawn and either
    leak the process or hit the cancel before the try-block had
    entered.

    Subprocess-keepalive: use ``time.sleep`` rather than
    ``sys.stdin.read``. Under xdist the worker's stdin is ``/dev/null``
    so ``stdin.read()`` returns immediately, the subprocess exits on
    its own, and the cancel races the natural completion path —
    ``terminate()`` then raises ``ProcessLookupError`` against a
    dead transport. A long sleep keeps the process alive until the
    test cancels.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        # Print one line so the runner enters the line-reading loop,
        # then sleep so the subprocess is still alive when we cancel.
        "import sys, time\nprint('starting...', flush=True)\ntime.sleep(60)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")

    proc_alive = asyncio.Event()
    captured: list[dict] = []
    real_fire = controller._db.bus.fire

    def _watch(event_type: EventType, data: dict) -> None:
        captured.append({"type": event_type, "data": data})
        if event_type == EventType.JOB_OUTPUT:
            proc_alive.set()
        real_fire(event_type, data)

    controller._db.bus.fire = _watch

    runner_task = asyncio.create_task(controller._run_queue())
    try:
        await asyncio.wait_for(proc_alive.wait(), timeout=10.0)
        assert controller.state.compile_lane.current_process is not None
        proc = controller.state.compile_lane.current_process

        # Cancel the runner task itself — this is the shutdown shape,
        # not the user-cancel one. Nothing is added to
        # ``_cancel_requested`` so the post-``proc.wait()`` branch
        # can't be the one that finalises the job.
        runner_task.cancel()
        with suppress(asyncio.CancelledError):
            await runner_task
    finally:
        # Defensive cleanup if the assertion path above bailed early.
        if not runner_task.done():
            runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await runner_task

    assert job.status == JobStatus.CANCELLED
    assert any(c["type"] == EventType.JOB_CANCELLED for c in captured)
    # ``proc.terminate()`` on POSIX puts the subprocess on a path to
    # exit; wait briefly so the assertion below isn't racy.
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    assert proc.returncode is not None, "runner-shutdown branch should have terminated the proc"
    # The discard is unconditional — it's a no-op when the id wasn't
    # in the set, which is exactly the shutdown case.
    assert job.job_id not in controller.state.cancel_requested


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
async def test_execute_job_runner_shutdown_kills_subprocess_group(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """Runner-shutdown cancellation walks the whole process group.

    Pre-refactor regression hazard: an earlier draft used
    ``proc.terminate()`` (signals only the python parent) inside
    the helper's ``CancelledError`` branch instead of
    ``_terminate_current_process`` (which uses
    ``terminate_subtree_with_grace`` →
    ``os.killpg(getpgid(pid), SIGTERM)`` to walk the group).
    With ``start_new_session=True`` the build's children
    (esphome → platformio → gcc / esptool) share a process group
    with the parent; the parent dies but children get orphaned
    and keep running. The previous runner-shutdown test would
    have passed with that buggy variant because it only asserts
    on ``proc.returncode`` of the python parent — by the time
    the test re-checks, the parent is dead, no child involved.

    Pin the group-walk by forking a real child. The parent
    spawns a long-running ``time.sleep`` subprocess, prints the
    child PID, then sleeps itself. After we cancel the runner,
    poll ``os.kill(child_pid, 0)`` — it raises
    ``ProcessLookupError`` only when the kernel has reaped the
    child, which only happens if the child got SIGTERM too.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        # Parent fork pattern: spawn a child sleeper that's NOT a
        # direct ``await proc.wait`` target — the runner only
        # tracks the python parent's pid. The cancel must walk
        # the process group to reach this child. ``start_new_session``
        # is False here so the child inherits the parent's group.
        # ``flush=True`` keeps the runner's line reader from
        # buffering the PID line past the cancel.
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(f'CHILD_PID={child.pid}', flush=True)\n"
        "time.sleep(60)\n",
    )
    _seed_yaml(tmp_path)

    await controller.compile(configuration="kitchen.yaml")

    child_seen = asyncio.Event()
    child_pid: list[int] = []
    real_fire = controller._db.bus.fire

    def _watch(event_type: EventType, data: dict) -> None:
        if event_type == EventType.JOB_OUTPUT:
            line = data.get("line", "")
            if "CHILD_PID=" in line:
                child_pid.append(int(line.split("=", 1)[1].strip()))
                child_seen.set()
        real_fire(event_type, data)

    controller._db.bus.fire = _watch

    runner_task = asyncio.create_task(controller._run_queue())
    try:
        await asyncio.wait_for(child_seen.wait(), timeout=10.0)
        assert child_pid, "test bug: never read CHILD_PID line"
        runner_task.cancel()
        with suppress(asyncio.CancelledError):
            await runner_task
    finally:
        if not runner_task.done():
            runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await runner_task

    # Poll until the child is reaped — group SIGTERM has a brief
    # propagation window, then the kernel cleans up the zombie
    # once the parent (also dead) is reaped.
    cpid = child_pid[0]
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        try:
            os.kill(cpid, 0)
        except ProcessLookupError:
            break  # child gone — group SIGTERM landed
        await asyncio.sleep(0.05)
    else:
        # Best-effort cleanup so we don't leak a runaway sleeper.
        with suppress(ProcessLookupError):
            os.kill(cpid, 9)
        pytest.fail(f"child {cpid} survived runner-shutdown cancel — group SIGTERM didn't reach it")


# ---------------------------------------------------------------------------
# Progress parsing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# RESET_BUILD_ENV — routes through the same subprocess pipeline
# ---------------------------------------------------------------------------


async def test_run_queue_routes_reset_build_env_through_clean_all_subprocess(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """A RESET_BUILD_ENV job shells out to ``esphome clean-all <config_dir>``.

    The runner has no per-job-type branch any more — every job
    routes through ``_build_command`` and ``create_subprocess_exec``.
    Mirrors the legacy ``EsphomeCleanAllHandler`` shape so the
    upstream ``writer.clean_all`` does the actual cleanup (every
    ``.esphome/`` subdir except ``storage/``, plus PlatformIO's
    real cache directories).

    Pin the dispatch by capturing the argv the spawned subprocess
    sees. A regression that re-introduces an inline rmtree
    (or that drops the ``clean-all`` mapping from ``_build_command``)
    surfaces here — argv either won't have ``clean-all`` or the
    config_dir positional, or the subprocess won't fire at all.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    _wire_real_queue(controller)
    # Subprocess writes its argv to a sidecar file we can inspect
    # after the run — stdin/stdout aren't a clean channel here
    # because the runner pattern-matches stdout for error markers.
    argv_log = tmp_path / "argv.json"
    _fake_esphome(
        controller,
        "import json, sys\n"
        f"open({str(argv_log)!r}, 'w').write(json.dumps(sys.argv))\n"
        "sys.exit(0)\n",
    )

    job = await controller.reset_build_env()
    captured = await _run_until_terminal(controller)

    assert job.status == JobStatus.COMPLETED
    assert job.exit_code == 0
    assert captured["job_completed"]

    argv = json.loads(argv_log.read_text(encoding="utf-8"))
    # argv[0] is CPython's ``-c`` placeholder; the rest is exactly
    # what ``_build_command`` produced for the queued job.
    assert argv[1:] == ["--dashboard", "clean-all", str(tmp_path)]


# ---------------------------------------------------------------------------
# Mid-run output trim + repeated error-pattern hit
# ---------------------------------------------------------------------------


async def test_compile_inflight_output_trims_when_cap_exceeded(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Builds that stream past the in-flight cap have ``output`` trimmed mid-run.

    Without the in-flight trim a chatty build (PlatformIO retry
    loop, esptool stuck on a repeating message) holds every line
    in memory until the subprocess exits — only the post-exit
    ``finally``-block trim ever runs. The dashboard process OOMs
    first.

    Pin specifically the *mid-run* trim path: spy on
    ``_trim_job_output`` and assert the call from inside the
    streaming loop fires with ``keep=_INFLIGHT_TRIM_KEEP`` and
    arrives while ``job.status`` is still ``RUNNING``. A naive
    end-state assertion (``len(job.output) <= keep``) would
    silently pass on a regression that dropped the mid-run trim
    because the ``finally``-block ``_trim_job_output(job)`` call
    runs anyway.
    """
    real_trim = helpers_module._trim_job_output
    inflight_trim_calls: list[tuple[JobStatus, int, int | None]] = []

    def _spy_trim(job: Any, *, keep: int | None = None) -> None:
        # Capture the call's keep= kwarg AND the job status at the
        # moment of the call. Mid-run calls are RUNNING + keep set;
        # the finally-block call is COMPLETED + keep unset.
        inflight_trim_calls.append((job.status, len(job.output), keep))
        if keep is None:
            real_trim(job)
        else:
            real_trim(job, keep=keep)

    # Patch both surfaces: the post-exit ``finally``-block call
    # resolves through ``lifecycle.end_run``, while the mid-run trim
    # lands via the shared ``_ingest_output_line`` helper in
    # ``helpers.py`` — each calls ``_trim_job_output`` from its own
    # module scope. Without patching both, the mid-run branch escapes
    # the spy and the regression check goes silent.
    monkeypatch.setattr(lifecycle_module, "_trim_job_output", _spy_trim)
    monkeypatch.setattr(helpers_module, "_trim_job_output", _spy_trim)

    excess = 200
    total = _MAX_OUTPUT_LINES_INFLIGHT + excess
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        f"import sys\nfor i in range({total}):\n    print(f'INFO line {{i}}')\nsys.exit(0)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")
    await _run_until_terminal(controller)

    assert job.status == JobStatus.COMPLETED
    # At least one trim call landed mid-run (status=RUNNING, keep
    # set to the in-flight constant). Without the mid-run trim,
    # only the post-exit COMPLETED + keep=None call would appear.
    inflight = [c for c in inflight_trim_calls if c[2] == _INFLIGHT_TRIM_KEEP]
    assert inflight, (
        "no in-flight trim observed — the mid-run trim branch is dead; "
        f"calls were: {inflight_trim_calls}"
    )
    assert all(status is JobStatus.RUNNING for status, _, _ in inflight)


async def test_compile_repeats_error_pattern_short_circuits_check(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    """Once an error pattern is seen, subsequent lines bypass the pattern scan.

    ``_check_error`` flips ``has_error_in_output`` on the first
    match and short-circuits on every subsequent call so a build
    spamming error lines doesn't pay an O(patterns) loop per line.
    Drive two error-pattern lines then a clean exit; the FAILED
    verdict still lands and the second line follows the
    short-circuit branch (line 803 in controller.py).
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _fake_esphome(
        controller,
        "import sys\n"
        "print('Traceback (most recent call last):')\n"
        # First error pattern — flips the flag.
        "print(\"ModuleNotFoundError: No module named 'cryptography'\")\n"
        # Second error pattern — hits the short-circuit branch.
        "print(\"ImportError: cannot import name 'foo'\")\n"
        "sys.exit(0)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")
    await _run_until_terminal(controller)

    assert job.status == JobStatus.FAILED
    assert job.exit_code == 0


async def test_compile_cr_progress_lines_collapsed_in_storage(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Path
) -> None:
    r"""``\r``-terminated progress chunks are collapsed at storage time.

    ESP-IDF / ninja builds emit a few thousand "[N/total] Compiling
    …\r" / "Linking …\r" updates that each overwrite the previous
    on-screen line. Without storage-side collapse, ``job.output``
    retains every chunk — the retained 2000-line tail fills with
    overwritten progress lines so a real error close to the end of
    the build falls outside the window during historical replay.

    This test pins the collapse rule:

    - Sequence of CR-terminated progress chunks for the same line
      slot ends with only the *last* chunk stored.
    - Live ``JOB_OUTPUT`` events still fire once per input chunk so
      followers' progress indicators animate; only ``job.output``
      (the historical-replay source) is collapsed.
    - A regular ``\n``-terminated log line after a CR-terminated
      chunk replaces it — matches the frontend's visual-line
      semantics, where a non-``\n`` follow-up pops the progress
      line.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    # Emit three CR-terminated progress chunks, then a real log line.
    # ``PYTHONUNBUFFERED=1`` is set in production by ``_execute_job``
    # so each ``write`` lands as a separate read on our pipe. Use
    # ``stdout.buffer.write`` so Windows text-mode translation
    # doesn't turn the trailing ``\n`` into ``\r\n`` on us.
    _fake_esphome(
        controller,
        "import sys\n"
        "sys.stdout.buffer.write(b'[1/3] Compiling a.o\\r')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdout.buffer.write(b'[2/3] Compiling b.o\\r')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdout.buffer.write(b'[3/3] Compiling c.o\\r')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdout.buffer.write(b'INFO Compile finished.\\n')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.exit(0)\n",
    )
    _seed_yaml(tmp_path)

    job = await controller.compile(configuration="kitchen.yaml")
    captured = await _run_until_terminal(controller)

    assert job.status == JobStatus.COMPLETED

    # Live followers see every chunk — the wire protocol isn't
    # affected by storage-side collapse.
    live_lines = [d["line"] for d in captured["job_output"]]
    assert "[1/3] Compiling a.o\r" in live_lines
    assert "[2/3] Compiling b.o\r" in live_lines
    assert "[3/3] Compiling c.o\r" in live_lines
    assert "INFO Compile finished.\n" in live_lines

    # ``job.output`` retains only the final state. The three CR
    # chunks collapse into the last one, then the trailing
    # ``\n``-terminated log line pops it (mirroring the frontend's
    # "non-``\n`` follow-up replaces the CR line" rule).
    assert "[1/3] Compiling a.o\r" not in job.output
    assert "[2/3] Compiling b.o\r" not in job.output
    assert "[3/3] Compiling c.o\r" not in job.output
    assert job.output[-1] == "INFO Compile finished.\n"
