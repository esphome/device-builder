"""End-to-end coverage for ``FirmwareController.clear``.

The handler is small but threads three subtle invariants the
dashboard's "Clear finished" button relies on:

- Default (``status=None``) removes *only* terminal jobs
  (COMPLETED / FAILED / CANCELLED) — never the user's
  in-progress ones. Pinned because a future "remove all" copy
  paste would silently nuke active builds.
- ``status`` filters by exact match. ``JobStatus`` is a
  ``StrEnum``, so the WS layer's bare-string ``"completed"``
  payload should compare equal to ``JobStatus.COMPLETED``.
- ``_persist_jobs`` runs after every clear so the metadata file
  on disk catches up with the in-memory map. Without that,
  a restart would resurrect the cleared jobs from the persisted
  history.

These tests pin all three so a regression in any direction
surfaces immediately.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from esphome_device_builder.models import FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory


def _job(job_id: str, status: JobStatus, *, job_type: JobType = JobType.COMPILE) -> FirmwareJob:
    """Minimal ``FirmwareJob`` with the surface ``clear`` reads (``status``)."""
    return FirmwareJob(
        job_id=job_id,
        configuration=f"{job_id}.yaml",
        job_type=job_type,
        status=status,
    )


# ---------------------------------------------------------------------------
# Default behaviour: status=None
# ---------------------------------------------------------------------------


async def test_clear_default_removes_all_terminal_states(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``clear()`` with no args removes every terminal job (COMPLETED/FAILED/CANCELLED).

    Pin all three terminal states in one test so a regression that
    forgets to include any one of them in ``TERMINAL_JOB_STATUSES``
    surfaces here regardless of which state was missed.
    """
    controller = firmware_controller_factory(
        _job("c", JobStatus.COMPLETED),
        _job("f", JobStatus.FAILED),
        _job("x", JobStatus.CANCELLED),
        with_settings=False,
    )

    await controller.clear()

    assert await controller.get_jobs() == []


async def test_clear_default_keeps_queued_and_running_jobs(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Active jobs (QUEUED / RUNNING) survive the default clear.

    The "Clear finished" button must never remove a build the
    user is still waiting on. Without this assertion, a regression
    that defaulted to "remove all" would silently nuke the queue
    and follow_job sessions would be left dangling.
    """
    controller = firmware_controller_factory(
        _job("q", JobStatus.QUEUED),
        _job("r", JobStatus.RUNNING),
        _job("c", JobStatus.COMPLETED),
        with_settings=False,
    )

    await controller.clear()

    assert {j.job_id for j in await controller.get_jobs()} == {"q", "r"}


async def test_clear_default_with_no_terminal_jobs_is_noop(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Empty terminal-set → nothing removed, but ``_persist_jobs`` still runs.

    Pinned because the default branch's filter list ends up empty
    here — a sloppy refactor that skipped persist when the list
    was empty would leak a stale on-disk file when the user
    *did* clear something earlier in the same session.
    """
    controller = firmware_controller_factory(
        _job("q", JobStatus.QUEUED),
        _job("r", JobStatus.RUNNING),
        with_settings=False,
    )

    await controller.clear()

    assert {j.job_id for j in await controller.get_jobs()} == {"q", "r"}
    controller._persist_jobs.assert_awaited_once()


# ---------------------------------------------------------------------------
# Filtered: status=...
# ---------------------------------------------------------------------------


async def test_clear_with_specific_status_removes_only_that_status(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``clear(status=COMPLETED)`` leaves FAILED and CANCELLED alone.

    The "Clear succeeded" / "Clear failed" buttons feed this path;
    without exact-match filtering they'd nuke the wrong category
    and the user would lose history they wanted to keep.
    """
    controller = firmware_controller_factory(
        _job("c1", JobStatus.COMPLETED),
        _job("c2", JobStatus.COMPLETED),
        _job("f", JobStatus.FAILED),
        _job("x", JobStatus.CANCELLED),
        with_settings=False,
    )

    await controller.clear(status=JobStatus.COMPLETED)

    assert {j.job_id for j in await controller.get_jobs()} == {"f", "x"}


async def test_clear_with_status_string_matches_enum_value(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The WS layer passes ``status`` as a bare string; equality must hold.

    ``JobStatus`` is a ``StrEnum`` so ``JobStatus.COMPLETED ==
    "completed"`` is true at runtime — pin that contract because
    every ``firmware/clear`` call from the frontend lands here as
    a string. A future refactor that switched to a non-string
    enum would silently make this comparison false and the
    string-status branch would no-op.
    """
    controller = firmware_controller_factory(
        _job("c", JobStatus.COMPLETED),
        _job("f", JobStatus.FAILED),
        with_settings=False,
    )

    await controller.clear(status="completed")

    assert {j.job_id for j in await controller.get_jobs()} == {"f"}


async def test_clear_with_status_can_remove_active_jobs(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Explicit ``status=RUNNING`` removes that exact state.

    The default path protects active jobs, but the explicit-status
    path is a power-user tool — a stuck RUNNING ghost (e.g. the
    runner crashed mid-job and the status didn't get flipped to
    FAILED) is a real recovery scenario. Pin the contract that
    the filter is applied verbatim, not intersected with terminal.
    """
    controller = firmware_controller_factory(
        _job("r", JobStatus.RUNNING),
        _job("q", JobStatus.QUEUED),
        _job("c", JobStatus.COMPLETED),
        with_settings=False,
    )

    await controller.clear(status=JobStatus.RUNNING)

    assert {j.job_id for j in await controller.get_jobs()} == {"q", "c"}


async def test_clear_with_status_no_matches_is_noop(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """An unmatched status is a no-op (still persists the unchanged map)."""
    controller = firmware_controller_factory(
        _job("c", JobStatus.COMPLETED),
        with_settings=False,
    )

    await controller.clear(status=JobStatus.FAILED)

    assert {j.job_id for j in await controller.get_jobs()} == {"c"}
    controller._persist_jobs.assert_awaited_once()


# ---------------------------------------------------------------------------
# Persistence + handler hygiene
# ---------------------------------------------------------------------------


async def test_clear_persists_after_removal(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``_persist_jobs`` is awaited after the in-memory delete.

    Without persist, a restart would resurrect cleared jobs from
    the on-disk metadata. Asserts both the call and the order
    (persist runs *after* the deletes — the map must be the
    cleared shape when persist serialises it).
    """
    controller = firmware_controller_factory(
        _job("c", JobStatus.COMPLETED),
        _job("q", JobStatus.QUEUED),
        with_settings=False,
    )
    seen_ids_at_persist: set[str] = set()

    async def _capture() -> None:
        seen_ids_at_persist.update(j.job_id for j in await controller.get_jobs())

    controller._persist_jobs = AsyncMock(side_effect=_capture)

    await controller.clear()

    controller._persist_jobs.assert_awaited_once()
    # Snapshot at persist time has the queued job only — not the
    # pre-delete shape.
    assert seen_ids_at_persist == {"q"}


async def test_clear_accepts_arbitrary_kwargs(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``**kwargs`` lets the WS dispatcher's keyword spread through unread fields.

    Same contract as every other ``firmware/*`` handler. A regression
    that tightens the signature would break WS calls that pass
    ``client`` / ``message_id`` / bookkeeping fields the handler
    doesn't read.
    """
    controller = firmware_controller_factory(
        _job("c", JobStatus.COMPLETED),
        with_settings=False,
    )

    await controller.clear(client=object(), message_id="m1", spurious=True)

    assert await controller.get_jobs() == []


async def test_clear_with_empty_jobs_map_is_noop(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Calling ``clear`` on an already-empty map doesn't crash and still persists."""
    controller = firmware_controller_factory(with_settings=False)

    await controller.clear()

    assert await controller.get_jobs() == []
    controller._persist_jobs.assert_awaited_once()
