"""Tests for ``FirmwareController.queue_status_snapshot``.

The snapshot is the only public read of the firmware queue's
RAM state — used by the remote-build controller's phase-5b
peer-link broadcast on every queue transition. Keep the three
state combinations (idle / queued-only / running) pinned here
so a future refactor that splits the runner slot or queue
representation has a one-stop check that the public shape
stays correct.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import FirmwareJob, JobType


def _make_controller() -> FirmwareController:
    db = MagicMock()
    return FirmwareController(db)


def _job(job_id: str = "j1") -> FirmwareJob:
    return FirmwareJob(job_id=job_id, configuration="kitchen.yaml", job_type=JobType.COMPILE)


def test_queue_status_snapshot_idle() -> None:
    """Cold controller: no current job, empty queue → idle."""
    controller = _make_controller()
    idle, running, queue_depth = controller.queue_status_snapshot()
    assert idle is True
    assert running is False
    assert queue_depth == 0


def test_queue_status_snapshot_running_only() -> None:
    """Runner busy with no backlog: idle=False, running=True, depth=0."""
    controller = _make_controller()
    controller._current_job = _job()
    idle, running, queue_depth = controller.queue_status_snapshot()
    assert idle is False
    assert running is True
    assert queue_depth == 0


def test_queue_status_snapshot_queued_but_not_running() -> None:
    """The pre-pickup window: ``_queue.put`` ran but ``_queue.get`` hasn't.

    Pins the asymmetry that motivated emitting all three fields:
    a phase-7 scheduler reading only ``running`` would treat a
    fully-loaded receiver as accepting more work during this
    window. The combination is real on the wire because
    ``submit_job`` puts onto the queue before the runner picks
    up the next item.
    """
    controller = _make_controller()
    controller._queue.put_nowait(_job("a"))
    controller._queue.put_nowait(_job("b"))
    idle, running, queue_depth = controller.queue_status_snapshot()
    assert idle is False
    assert running is False
    assert queue_depth == 2


def test_queue_status_snapshot_running_and_queued() -> None:
    """Runner busy AND backlog: idle=False, running=True, depth>0."""
    controller = _make_controller()
    controller._current_job = _job("active")
    controller._queue.put_nowait(_job("waiting"))
    idle, running, queue_depth = controller.queue_status_snapshot()
    assert idle is False
    assert running is True
    assert queue_depth == 1
