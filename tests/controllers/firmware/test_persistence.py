"""End-to-end coverage for firmware-job persistence across restarts.

Drives through the public API only — no private method names,
no on-disk format details — so the test contract is "what gets
queued before shutdown is queued after restart" rather than "the
metadata file has this exact key shape". An implementation
rewrite (separate jobs.json file, sqlite, whatever) keeps the
tests passing as long as the user-visible behaviour is preserved.

Pinned policy (esphome/device-builder#147):

- ``QUEUED`` → re-queue on next boot.
- ``RUNNING`` → ``FAILED`` with a "dashboard restarted" reason.
  The subprocess died with the dashboard, so silently
  re-queueing would re-run a build the user didn't ask for.
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
async def test_running_job_finalises_as_failed_after_restart(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    patch_runtime: None,
) -> None:
    """A ``RUNNING`` job at shutdown comes back ``FAILED`` with a reason.

    The subprocess died with the dashboard, so we can't resume
    it; silently re-queueing would re-run a build the user
    didn't ask for. The frontend's recent-jobs panel renders
    the failure with the reason so the user can decide whether
    to retry.

    Phase 1 has to mutate ``self._jobs[...].status`` directly
    because there's no public API for "make the queue runner
    pick this up" without actually running ``esphome`` — but
    everything *after* that goes through public methods (the
    cancel-style status flip + ``_persist_jobs`` is invoked
    via ``cancel`` semantics through ``clear`` / similar).
    """
    (tmp_path / "kitchen.yaml").write_text("")
    writer = _persistent_controller(firmware_controller_factory)
    queued = await writer.compile(configuration="kitchen.yaml")
    # Simulate the runner having picked up the job before the
    # dashboard goes down. The status flip + persist is what the
    # real runner would have done; we trigger persistence via a
    # public path (``cancel`` would write — but cancelling
    # doesn't put it in RUNNING). Persist explicitly through the
    # method that *is* user-driven: another submission against
    # the same config supersedes the QUEUED entry, but that
    # doesn't help either. Easiest: force the in-memory state
    # the runner would set, then trigger any persist via a
    # follow-up public call.
    writer._jobs[queued.job_id].status = JobStatus.RUNNING
    # Persist by submitting any other job — its enqueue flushes.
    (tmp_path / "garage.yaml").write_text("")
    await writer.compile(configuration="garage.yaml")

    reader = await _restart(firmware_controller_factory)

    restored_jobs = {j.job_id: j for j in await reader.get_jobs()}
    assert queued.job_id in restored_jobs
    restored = restored_jobs[queued.job_id]
    assert restored.status == JobStatus.FAILED
    assert restored.completed_at is not None
    assert restored.error is not None
    assert "Dashboard restarted" in restored.error
    # The other (genuinely queued) job is still queued.
    other = next(j for j in restored_jobs.values() if j.job_id != queued.job_id)
    assert other.status == JobStatus.QUEUED


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
