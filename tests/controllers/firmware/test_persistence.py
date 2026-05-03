"""End-to-end coverage for firmware-job persistence across restarts.

Drives through the public API for the *contract* assertions
(``compile`` / ``cancel`` / ``start`` / ``get_jobs``) so an
implementation rewrite (separate ``jobs.json`` file, sqlite,
whatever) keeps the tests passing as long as the user-visible
behaviour is preserved. Two acknowledged seams remain:

- The ``RUNNING``-carryover test mutates ``writer._jobs[id]``
  directly to simulate "the runner was mid-build when the
  dashboard died". There's no public API for putting a job
  into ``RUNNING`` status without spawning a real ``esphome``
  subprocess; the load-side rewrite is what's actually pinned
  and that runs through the public ``start()`` path.
- The corrupt-entry test surgically pokes
  ``.device-builder.json`` because by design no public API
  writes garbage into the persistence layer. The test
  discovers the jobs key by suffix-match (``firmware_jobs``)
  rather than importing the constant, so a rename of
  ``_JOBS_KEY`` doesn't trip the test.

Some tests also assert on ``_queue.put`` (which the conftest
factory installs as ``AsyncMock``) to confirm the load path
actually exchanged the job onto the queue — ``get_jobs()``
alone reads ``self._jobs`` and would pass even if the runner
never saw the job. That's a defence-in-depth check on the
``"will run after restart"`` half of the policy.

Pinned policy (esphome/device-builder#147):

- ``QUEUED`` and ``RUNNING`` → re-queue. Re-running an
  interrupted build is idempotent at worst (the rebuilt
  firmware ends up identical), the user pays a couple minutes
  of compile time, no harm done. ``RUNNING`` jobs go through
  ``FirmwareJob.reset()`` first so the rebuild's ``progress``
  / ``exit_code`` / ``error`` fields don't leak the crashed
  run's state — but the original log is kept as diagnostic
  history with a separator marker.
- Terminal (``COMPLETED`` / ``FAILED`` / ``CANCELLED``) → load
  into the recent-jobs panel; don't re-queue.

Phase 1 of every test queues / mutates jobs through public
methods (``compile`` / ``cancel`` / ``clear`` …) — those
trigger persistence as a side effect. Phase 2 spins up a fresh
controller pointing at the same config dir and calls
``start()`` to trigger the load. Phase 3 asserts via
``get_jobs`` and reads ``status`` / ``error`` off the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import FirmwareJob, JobStatus
from tests.controllers.firmware.conftest import FirmwareControllerFactory


@pytest.fixture
def patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the subprocess bits of ``start()`` so it runs without spawning.

    ``start()`` calls ``_find_esphome_cmd`` (which probes
    ``sys.executable``) and ``_verify_esphome_importable`` (which
    spawns ``esphome --version``). Neither is the subject of this
    test file; replace both so ``start()``'s persistence-load
    branch is the only thing exercised.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.controller._find_esphome_cmd",
        lambda: ["fake-esphome"],
    )

    async def _verify(_cmd: list[str]) -> tuple[bool, str]:
        return True, "fake-version"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.controller._verify_esphome_importable",
        _verify,
    )


def _persistent_controller(
    factory: FirmwareControllerFactory,
    **overrides: Any,
) -> FirmwareController:
    """Build a controller that actually writes to disk.

    The conftest factory's default ``with_real_persistence=False``
    installs an ``AsyncMock`` for ``_persist_jobs``; the
    persistence tests need the real method, plus the queue kit
    so submission handlers reach the persist path.
    """
    return factory(with_queue=True, with_real_persistence=True, **overrides)


async def _restart(
    factory: FirmwareControllerFactory,
) -> FirmwareController:
    """Spin up a fresh controller and run ``start()`` against the same config dir.

    The factory shares a single ``tmp_path`` across calls within
    one test (pytest's ``tmp_path`` fixture is per-test, not
    per-controller-call), so calling the factory a second time
    yields a controller whose settings point at the same config
    dir as the first.
    """
    fresh = _persistent_controller(factory)
    await fresh.start()
    return fresh


# ---------------------------------------------------------------------------
# Round-trip via public API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_job_survives_dashboard_restart(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    patch_runtime: None,
) -> None:
    """A ``QUEUED`` job submitted before restart is re-queued after.

    User flow: queue a compile, dashboard goes down before the
    runner picks it up, dashboard comes back. The job should be
    waiting where they left it.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    writer = _persistent_controller(firmware_controller_factory)
    queued = await writer.compile(configuration="kitchen.yaml")

    reader = await _restart(firmware_controller_factory)

    after_restart = await reader.get_jobs()
    assert len(after_restart) == 1
    assert after_restart[0].job_id == queued.job_id
    assert after_restart[0].status == JobStatus.QUEUED
    assert after_restart[0].configuration == "kitchen.yaml"
    # Confirm the load path actually put the job back on the queue,
    # not just into ``self._jobs``. ``get_jobs`` reads the in-memory
    # map, which would still pass if ``_load_jobs`` forgot to
    # ``await self._queue.put(...)``.
    reader._queue.put.assert_awaited_once()
    queued_arg = reader._queue.put.await_args.args[0]
    assert queued_arg.job_id == queued.job_id


@pytest.mark.asyncio
async def test_running_job_re_queues_with_clean_state_after_restart(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    patch_runtime: None,
) -> None:
    """A ``RUNNING`` job at shutdown comes back ``QUEUED`` with per-run state cleared.

    The user asked for the build; even though the subprocess
    died with the dashboard, the request is still pending in
    their head. Worst case the rebuild produces the same
    firmware that was already on the device — that's
    idempotent, the user pays a couple minutes of compile time,
    no harm done.

    Per-run *state* from the crashed run is cleared (``progress``
    / ``error`` / ``started_at`` / ``completed_at`` /
    ``exit_code``) so the rebuild's status display starts
    fresh. The pre-crash ``output`` is *retained* as
    diagnostic history with a recovery-marker line appended —
    a follower tailing the merged buffer can see exactly
    where the rebuild starts. The marker is what stops the
    "two builds glued together with no demarcation" UX
    problem; without it the rebuild's lines would silently
    concatenate onto whatever the crash left behind.

    Phase 1 has to mutate ``self._jobs[...].status`` directly to
    simulate the runner having picked up the job before the
    dashboard went down — there's no public API for "make the
    runner mid-build" without spawning a real ``esphome``.
    Phase 2's load behaviour is what's actually pinned and that
    runs through the public ``start()`` path.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    writer = _persistent_controller(firmware_controller_factory)
    queued = await writer.compile(configuration="kitchen.yaml")
    # Simulate the runner having picked up the job mid-build —
    # the status flip + per-run state are what the real runner
    # would have set on its own. Persistence happens implicitly
    # via the next ``compile`` submission's enqueue path.
    in_flight = writer._jobs[queued.job_id]
    in_flight.status = JobStatus.RUNNING
    in_flight.output = ["compile in progress …\n", "src/main.cpp\n"]
    in_flight.progress = 47
    in_flight.started_at = "2026-01-01T00:00:00+00:00"
    (tmp_path / "garage.yaml").write_text("")
    await writer.compile(configuration="garage.yaml")

    reader = await _restart(firmware_controller_factory)

    restored_jobs = {j.job_id: j for j in await reader.get_jobs()}
    assert queued.job_id in restored_jobs
    restored = restored_jobs[queued.job_id]
    # Re-queued, not failed.
    assert restored.status == JobStatus.QUEUED
    # Pre-crash log retained as diagnostic history, with a
    # marker line showing where the rebuild begins.
    assert "compile in progress …\n" in restored.output
    assert "src/main.cpp\n" in restored.output
    assert any("dashboard restarted mid-build" in line for line in restored.output)
    # Other per-run state cleared so the rebuild's status display
    # shows fresh values.
    assert restored.progress is None
    assert restored.error is None
    assert restored.started_at is None
    assert restored.completed_at is None
    assert restored.exit_code is None
    # Job identity preserved.
    assert restored.configuration == "kitchen.yaml"
    # The load path put both jobs (the running carryover + the
    # follow-up queued sibling) onto the queue — confirms the
    # carryover will actually run, not just sit ``QUEUED`` in
    # ``self._jobs`` forever.
    queued_ids = {call.args[0].job_id for call in reader._queue.put.await_args_list}
    assert queued.job_id in queued_ids


@pytest.mark.asyncio
async def test_resumed_running_job_completes_on_next_run(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    patch_runtime: None,
) -> None:
    """A re-queued ``RUNNING`` carryover gets dequeued + driven to ``COMPLETED``.

    This is the half of the contract the previous test doesn't
    cover: not just "the job came back ``QUEUED`` with cleared
    state" but "the queue runner *actually picks it up* and
    drives it to a terminal state on the next run". Without
    this the recovery path is half-implemented — ``reset`` could
    be wrong in a way that blocks ``_execute_job``, and the job
    would sit in ``QUEUED`` forever after restart.

    Drives a real ``_run_queue`` task with ``_execute_job``
    mocked to just flip the status (no real ``esphome`` spawn).
    The ``create_background_task`` stub on ``_db`` is replaced
    with one that actually schedules so the runner consumes
    the queue.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    writer = _persistent_controller(firmware_controller_factory)
    queued = await writer.compile(configuration="kitchen.yaml")
    in_flight = writer._jobs[queued.job_id]
    in_flight.status = JobStatus.RUNNING
    in_flight.output = ["compile in progress …\n"]
    in_flight.progress = 47
    in_flight.started_at = "2026-01-01T00:00:00+00:00"
    # Persist via a follow-up enqueue (compile of a different config).
    (tmp_path / "garage.yaml").write_text("")
    await writer.compile(configuration="garage.yaml")

    reader = _persistent_controller(firmware_controller_factory)
    # The factory's ``_queue`` is an ``AsyncMock`` — fine for tests
    # that just assert on calls, but the runner needs a real
    # ``asyncio.Queue`` so ``_load_jobs``'s ``put`` and
    # ``_run_queue``'s ``get`` actually exchange jobs.
    reader._queue = asyncio.Queue()

    # Replace _execute_job with a fast COMPLETED transition so we
    # don't actually spawn ``esphome``. The runner's loop calls
    # this, awaits it, then loops back for the next item.
    async def _fake_execute(job: FirmwareJob) -> None:
        job.status = JobStatus.COMPLETED
        job.exit_code = 0
        job.completed_at = "2026-01-01T01:00:00+00:00"
        await reader._persist_jobs()

    reader._execute_job = _fake_execute  # type: ignore[method-assign]

    # Schedule the runner for real. Track the task for cleanup.
    runner_tasks: list[asyncio.Task[None]] = []

    def _real_schedule(coro: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        runner_tasks.append(task)
        return task

    reader._db.create_background_task = _real_schedule

    await reader.start()
    # Wait for the runner to drain both queued jobs.
    try:
        async with asyncio.timeout(2.0):
            while True:
                statuses = {j.job_id: j.status for j in await reader.get_jobs()}
                if all(s == JobStatus.COMPLETED for s in statuses.values()):
                    break
                await asyncio.sleep(0.01)
    finally:
        for task in runner_tasks:
            task.cancel()
        await asyncio.gather(*runner_tasks, return_exceptions=True)

    # The carryover and the follow-up both reached COMPLETED.
    final = {j.job_id: j for j in await reader.get_jobs()}
    assert final[queued.job_id].status == JobStatus.COMPLETED
    assert final[queued.job_id].exit_code == 0
    # The pre-crash log + recovery marker survived the rebuild
    # (the fake ``_execute_job`` doesn't append, but ``reset``
    # added the marker on load).
    assert any("dashboard restarted mid-build" in line for line in final[queued.job_id].output)


@pytest.mark.asyncio
async def test_cancelled_job_survives_restart_without_being_requeued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    patch_runtime: None,
) -> None:
    """Cancelled jobs persist with status ``CANCELLED`` and don't re-queue.

    The recent-jobs panel renders out of ``get_jobs()``; a
    dashboard restart shouldn't blank the cancellation history.
    Equally, a cancelled job mustn't come back as ``QUEUED`` on
    next boot — the user already said no.

    ``cancel`` is the only terminal path with a public API;
    ``COMPLETED`` and ``FAILED`` are runner-driven (require a
    real subprocess) and aren't exercised end-to-end here. The
    status-routing branch in the loader handles them
    uniformly with ``CANCELLED`` per the pinned policy.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    writer = _persistent_controller(firmware_controller_factory)
    queued = await writer.compile(configuration="kitchen.yaml")
    await writer.cancel(job_id=queued.job_id)

    reader = await _restart(firmware_controller_factory)
    restored_jobs = {j.job_id: j for j in await reader.get_jobs()}
    assert queued.job_id in restored_jobs
    assert restored_jobs[queued.job_id].status == JobStatus.CANCELLED
    # Terminal jobs must NOT be re-queued. ``get_jobs`` showing the
    # job at status ``CANCELLED`` would pass even if the loader
    # accidentally put it on the queue too — assert the queue
    # interaction explicitly.
    reader._queue.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_start_with_no_metadata_file_is_empty(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_runtime: None,
) -> None:
    """First-run UX: no metadata file → ``get_jobs()`` returns ``[]`` after start.

    A fresh dashboard install has no ``.device-builder.json``
    yet. Startup must not raise; the recent-jobs panel just
    shows the empty state.
    """
    fresh = await _restart(firmware_controller_factory)
    assert await fresh.get_jobs() == []


# ---------------------------------------------------------------------------
# Direct seeding for cases that aren't easily reachable via public API
# ---------------------------------------------------------------------------
#
# The malformed-entry recovery branch can't be exercised through
# the public API — by design, the public API only writes
# well-formed entries. Drive it by writing a corrupt entry
# directly to ``.device-builder.json`` and asserting that the
# dashboard recovers and surfaces the rest of the queue.


@pytest.mark.asyncio
async def test_corrupt_entry_in_metadata_does_not_block_startup(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    patch_runtime: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed persisted entry logs a warning; the rest of the queue loads.

    Defensive: a half-written persistence file (dashboard
    killed mid-write) or an upstream schema change shouldn't
    make the queue unrecoverable on next start. Write a good
    queued job through the public API first, then surgically
    corrupt one entry by appending a malformed payload — the
    dashboard should boot with just the good entry.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    writer = _persistent_controller(firmware_controller_factory)
    good = await writer.compile(configuration="kitchen.yaml")

    # Surgically inject a corrupt entry alongside the good one.
    metadata_path = tmp_path / ".device-builder.json"
    raw = json.loads(metadata_path.read_text())
    jobs_key = next(k for k in raw if k.endswith("firmware_jobs"))
    raw[jobs_key].append({"this_is_not_a_valid_firmware_job": True})
    metadata_path.write_text(json.dumps(raw))

    with caplog.at_level(logging.WARNING):
        reader = await _restart(firmware_controller_factory)

    surviving = await reader.get_jobs()
    assert len(surviving) == 1
    assert surviving[0].job_id == good.job_id
    assert any("Failed to restore job" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    "garbage",
    ["not-a-dict", 42, None, ["nested", "list"]],
)
@pytest.mark.asyncio
async def test_non_dict_entry_in_metadata_does_not_crash_warning_path(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    patch_runtime: None,
    caplog: pytest.LogCaptureFixture,
    garbage: object,
) -> None:
    """A non-dict primitive in the persistence list logs a warning, doesn't crash.

    The original ``except`` branch called ``job_data.get("job_id", "?")``
    unconditionally, which raises ``AttributeError`` when
    ``job_data`` is a string / int / ``None`` / list — turning
    "skip and continue" into "abort startup". Pin the
    isinstance guard so a future refactor that drops it shows
    up here.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    writer = _persistent_controller(firmware_controller_factory)
    good = await writer.compile(configuration="kitchen.yaml")

    metadata_path = tmp_path / ".device-builder.json"
    raw = json.loads(metadata_path.read_text())
    jobs_key = next(k for k in raw if k.endswith("firmware_jobs"))
    raw[jobs_key].append(garbage)
    metadata_path.write_text(json.dumps(raw))

    with caplog.at_level(logging.WARNING):
        reader = await _restart(firmware_controller_factory)

    # Good entry survived; non-dict garbage logged a warning
    # naming the offending raw repr.
    surviving = await reader.get_jobs()
    assert len(surviving) == 1
    assert surviving[0].job_id == good.job_id
    assert any(
        "Failed to restore job" in rec.message and "non-dict entry" in rec.message
        for rec in caplog.records
    )
