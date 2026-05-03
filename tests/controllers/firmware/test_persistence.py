"""End-to-end coverage for firmware-job persistence across restarts.

Drives through the public API only — no private method names,
no on-disk format details — so the test contract is "what gets
queued before shutdown is queued after restart" rather than "the
metadata file has this exact key shape". An implementation
rewrite (separate jobs.json file, sqlite, whatever) keeps the
tests passing as long as the user-visible behaviour is preserved.

Pinned policy (esphome/device-builder#147):

- ``QUEUED`` and ``RUNNING`` → re-queue. Re-running an
  interrupted build is idempotent at worst (the rebuilt
  firmware ends up identical), the user pays a couple minutes
  of compile time, no harm done. ``RUNNING`` jobs go through
  ``_reset_job_for_recovery`` first so the rebuild's
  ``progress`` / ``exit_code`` / ``error`` fields don't leak
  the crashed run's state — but the original log is kept as
  diagnostic history with a separator marker.
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

from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import JobStatus
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

    Per-run state from the crashed run is cleared
    (``output`` / ``progress`` / ``error`` / ``started_at`` /
    ``completed_at`` / ``exit_code``) so the re-run's log looks
    like a fresh build instead of being concatenated onto
    whatever the crash left in the buffer. ``_execute_job``
    appends rather than resets, so without the load-side clear
    a user tailing the re-run would see two builds' worth of
    output stitched together.

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
    import json
    import logging

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
