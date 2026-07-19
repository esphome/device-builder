"""Tests for ``FirmwareController.compile_queue_status``.

``compile_queue_status`` is the read the remote-build peer-link
broadcast keys on (a receiver only compiles), on every queue
transition. Keep the three
state combinations (idle / queued-only / running) pinned here
so a future refactor that splits the runner slot or queue
representation has a one-stop check that the public shape
stays correct.

The terminal-ordering tests at the bottom of the file pin the
*timing* contract: a JOB_COMPLETED / JOB_FAILED / JOB_CANCELLED
listener that reads ``compile_queue_status()`` inside the
synchronous ``bus.fire`` callback must see ``running=False``.
The remote-build controller's broadcaster does exactly this,
and an off-by-one ordering on the slot release used to leave
the offloader's ``_peer_queue_status`` cache frozen at
``running=True`` (silent-LOCAL fallback on every install after
the first remote build).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController, remote_runner
from esphome_device_builder.controllers.firmware._state import FirmwareState
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobStatus,
    JobType,
)


def _make_controller() -> FirmwareController:
    db = MagicMock()
    return FirmwareController(db)


def _job(job_id: str = "j1") -> FirmwareJob:
    return FirmwareJob(job_id=job_id, configuration="kitchen.yaml", job_type=JobType.COMPILE)


def test_compile_queue_status_idle() -> None:
    """Cold controller: no current job, empty queue → idle."""
    controller = _make_controller()
    idle, running, queue_depth = controller.compile_queue_status()
    assert idle is True
    assert running is False
    assert queue_depth == 0


def test_compile_queue_status_running_only() -> None:
    """Runner busy with no backlog: idle=False, running=True, depth=0."""
    controller = _make_controller()
    job = _job()
    controller.state.compile_lane.active[job.job_id] = job
    idle, running, queue_depth = controller.compile_queue_status()
    assert idle is False
    assert running is True
    assert queue_depth == 0


def test_compile_queue_status_queued_but_not_running() -> None:
    """The pre-pickup window: ``_queue.put`` ran but ``_queue.get`` hasn't.

    Pins the asymmetry that motivated emitting all three fields:
    a phase-7 scheduler reading only ``running`` would treat a
    fully-loaded receiver as accepting more work during this
    window. The combination is real on the wire because
    ``submit_job`` puts onto the queue before the runner picks
    up the next item.
    """
    controller = _make_controller()
    controller.state.compile_lane.queue.put_nowait(_job("a"))
    controller.state.compile_lane.queue.put_nowait(_job("b"))
    idle, running, queue_depth = controller.compile_queue_status()
    assert idle is False
    assert running is False
    assert queue_depth == 2


def test_compile_queue_status_running_and_queued() -> None:
    """Runner busy AND backlog: idle=False, running=True, depth>0."""
    controller = _make_controller()
    active = _job("active")
    controller.state.compile_lane.active[active.job_id] = active
    controller.state.compile_lane.queue.put_nowait(_job("waiting"))
    idle, running, queue_depth = controller.compile_queue_status()
    assert idle is False
    assert running is True
    assert queue_depth == 1


# ---------------------------------------------------------------------------
# Terminal-fire ordering: a listener that snapshots inside the fire
# must observe the post-terminal idle state, not the still-running
# snapshot. Pins the contract every terminal-fire site reaches
# through ``FirmwareController._finalize_terminal``.
# ---------------------------------------------------------------------------


def _make_controller_with_real_bus() -> FirmwareController:
    """Stub a controller with a real :class:`EventBus` for sync listener tests.

    ``MagicMock`` for the bus is enough for the snapshot tests
    above; the ordering tests need a real ``bus.fire`` so a
    listener installed via ``add_listener`` runs synchronously
    inside the fire (the production semantics the remote-build
    broadcaster relies on).
    """
    db = MagicMock()
    db.bus = EventBus()
    controller = FirmwareController.__new__(FirmwareController)
    controller.state = FirmwareState()
    controller._db = db
    controller.state.jobs = {}
    controller.state.compile_lane.queue = asyncio.Queue()
    controller.state.cancel_requested = set()
    controller.state.cancel_events = {}
    return controller


def _capture_snapshot_in_listener(
    controller: FirmwareController, event_type: EventType
) -> list[tuple[bool, bool, int]]:
    """Subscribe a listener to *event_type* that records ``compile_queue_status()``.

    Returns the list the listener appends into. The tests assert
    that the recorded tuple shows ``idle=True, running=False``
    (or whatever the post-terminal state is for the test's queue
    depth) — proving the slot release happened *before* the
    fire reached the listener.
    """
    captured: list[tuple[bool, bool, int]] = []

    def _listener(_event: object) -> None:
        captured.append(controller.compile_queue_status())

    controller._db.bus.add_listener(event_type, _listener)
    return captured


@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        (JobStatus.COMPLETED, EventType.JOB_COMPLETED),
        (JobStatus.FAILED, EventType.JOB_FAILED),
        (JobStatus.CANCELLED, EventType.JOB_CANCELLED),
    ],
)
def test_finalize_terminal_releases_slot_before_listener_fires(
    status: JobStatus, event_type: EventType
) -> None:
    """Listener-during-fire sees ``running=False`` for every terminal status.

    The bug this pins: the runner used to fire the terminal
    event while the lane slot was still claimed (the ``finally``
    cleanup ran *afterwards*). The remote-build broadcaster
    captured ``running=True`` and the offloader's
    ``_peer_queue_status`` cache froze there, silently routing
    every subsequent install to LOCAL.
    """
    controller = _make_controller_with_real_bus()
    job = _job()
    controller.state.compile_lane.active[job.job_id] = job
    controller.state.processes[job.job_id] = MagicMock()
    captured = _capture_snapshot_in_listener(controller, event_type)

    controller._finalize_terminal(job, status)

    assert captured == [(True, False, 0)]
    # And the slot stays released after the fire returns.
    assert not controller.state.compile_lane.active
    assert job.job_id not in controller.state.processes


def test_finalize_terminal_skips_release_when_job_not_active() -> None:
    """A finalise on a job with no lane slot leaves the running slot alone.

    The QUEUED-cancel path goes through ``cancel`` (not
    ``_finalize_terminal``), but the helper's job-id keying
    is still load-bearing: a future caller that passes a
    different job must not evict whatever's actually running.
    The listener still fires — just with the running slot
    intact.
    """
    controller = _make_controller_with_real_bus()
    running = _job("running")
    other = _job("other")
    controller.state.compile_lane.active[running.job_id] = running
    captured = _capture_snapshot_in_listener(controller, EventType.JOB_FAILED)

    controller._finalize_terminal(other, JobStatus.FAILED)

    assert captured == [(False, True, 0)]
    assert controller.state.compile_lane.active == {running.job_id: running}


def test_finalize_terminal_rejects_non_terminal_status() -> None:
    """Stamping a non-terminal status raises before the slot release.

    Mirrors ``FirmwareJob.mark_terminal``'s loud-fail guard — keeps
    a stray ``self._finalize_terminal(job, JobStatus.RUNNING)``
    from silently emitting a JOB_RUNNING event (which
    ``_STATUS_TO_TERMINAL_EVENT`` doesn't have a key for, so it
    would crash later with a less-actionable ``KeyError``).
    """
    controller = _make_controller_with_real_bus()
    job = _job()
    controller.state.compile_lane.active[job.job_id] = job

    with pytest.raises(ValueError, match="non-terminal status"):
        controller._finalize_terminal(job, JobStatus.RUNNING)
    # Slot intact — we raised before the release.
    assert job.job_id in controller.state.compile_lane.active


@pytest.mark.parametrize(
    ("status", "event_type", "fn_name"),
    [
        (JobStatus.COMPLETED, EventType.JOB_COMPLETED, "_finalize_success"),
        (JobStatus.FAILED, EventType.JOB_FAILED, "_fail_locally"),
    ],
)
def test_remote_runner_terminal_helpers_release_slot_before_fire(
    status: JobStatus, event_type: EventType, fn_name: str
) -> None:
    """The remote-runner finalise paths route slot release through the helper.

    On the offloader the local upload-after-remote-compile
    branch finalises through :func:`remote_runner._finalize_success`
    or :func:`remote_runner._fail_locally`; both go through
    :meth:`FirmwareController._finalize_terminal` so the
    listener-during-fire ordering matches the local subprocess
    path.
    """
    controller = _make_controller_with_real_bus()
    job = _job()
    controller.state.compile_lane.active[job.job_id] = job
    controller.state.processes[job.job_id] = MagicMock()
    captured = _capture_snapshot_in_listener(controller, event_type)

    if fn_name == "_finalize_success":
        remote_runner._finalize_success(controller, job)
    else:
        remote_runner._fail_locally(controller, job, reason="boom")

    assert captured == [(True, False, 0)]
    assert not controller.state.compile_lane.active
    assert job.job_id not in controller.state.processes
    assert job.status is status
    if status is JobStatus.FAILED:
        # ``_fail_locally`` stamps ``job.error`` before
        # ``_finalize_terminal``; the JOB_FAILED listener that
        # rides the broadcast sees the populated field.
        assert job.error == "remote build: boom"
    else:
        assert job.error is None


def test_compile_queue_status_ignores_a_busy_upload_lane() -> None:
    """A receiver uploading still advertises compile-lane idle to offloaders.

    The offloader keys on ``compile_queue_status`` precisely so an upload in
    flight on the other lane doesn't read as a fully-loaded receiver (the
    frozen-running silent-LOCAL-fallback bug).
    """
    controller = _make_controller()
    uploading = _job("uploading")
    controller.state.upload_lane.active[uploading.job_id] = uploading

    compile_status = controller.compile_queue_status()
    assert compile_status.idle is True
    assert compile_status.running is False
    assert compile_status.queue_depth == 0
