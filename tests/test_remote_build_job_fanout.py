"""
Tests for the receiver-side ``JOB_*`` event fan-out to peer-link frames.

Phase 5c-2b of issue #106. Pairs with the 5c-2a receiver-side
``submit_job`` accept path: 5c-2a queues a :class:`FirmwareJob`
with ``remote_peer`` + ``remote_job_id`` set; this module's
:class:`JobFanout` subscribes to firmware ``JOB_*`` events and
forwards remote-peer jobs as ``job_state_changed`` /
``job_output`` frames over the submitting peer-link session.

Driven against a real :class:`EventBus` so the listener
attach / fire / detach flow runs end-to-end. Sessions are
stubbed with a recording ``send_app_frame`` so the test can
assert on the wire-shape payloads without standing up the
full Noise encrypt path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.remote_build_job_fanout import JobFanout
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobOutputData,
    JobStatus,
    JobType,
)


def _make_session(*, dashboard_id: str = "alpha") -> Any:
    """Stub ``PeerLinkSession`` capturing send_app_frame payloads."""
    session = MagicMock()
    session.dashboard_id = dashboard_id
    session.send_app_frame = AsyncMock(return_value=True)
    return session


def _make_remote_job(
    *,
    job_id: str = "local-1",
    remote_peer: str = "alpha",
    remote_job_id: str = "wire-job",
    status: JobStatus = JobStatus.QUEUED,
    error: str | None = None,
) -> FirmwareJob:
    """Build a remote-peer ``FirmwareJob`` for fan-out tests."""
    return FirmwareJob(
        job_id=job_id,
        configuration=".esphome/.remote_builds/alpha/kitchen/kitchen.yaml",
        job_type=JobType.COMPILE,
        status=status,
        remote_peer=remote_peer,
        remote_job_id=remote_job_id,
        error=error,
    )


def _make_controller(*, bus: EventBus, sessions: dict[str, Any] | None = None) -> Any:
    """Stub :class:`RemoteBuildController` with the attributes ``JobFanout`` reads.

    ``_db.bus`` for listener attach, ``_peer_link_sessions``
    for the per-event session lookup, ``_db.firmware._jobs``
    for the JOB_OUTPUT lookup path, and
    ``_db.create_background_task`` to drive the background
    send. The send is awaited synchronously via the captured
    coroutine list so the test can assert deterministically
    without timing-based polling.
    """
    background_tasks: list[Any] = []

    def _create_background_task(coro: Any) -> Any:
        background_tasks.append(coro)
        return MagicMock()

    db = MagicMock()
    db.bus = bus
    db.create_background_task = _create_background_task

    firmware = MagicMock()
    firmware._jobs = {}
    db.firmware = firmware

    controller = MagicMock()
    controller._db = db
    controller._peer_link_sessions = sessions or {}
    controller.background_tasks = background_tasks
    return controller


async def _drain_background(controller: Any) -> None:
    """Run every coroutine queued via ``create_background_task``."""
    for coro in controller.background_tasks:
        await coro
    controller.background_tasks.clear()


# ---------------------------------------------------------------------------
# Lifecycle event fan-out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "status_field", "expected_status"),
    [
        (EventType.JOB_STARTED, JobStatus.RUNNING, "running"),
        (EventType.JOB_COMPLETED, JobStatus.COMPLETED, "completed"),
        (EventType.JOB_FAILED, JobStatus.FAILED, "failed"),
        (EventType.JOB_CANCELLED, JobStatus.CANCELLED, "cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_lifecycle_event_fans_out_as_job_state_changed(
    event_type: EventType, status_field: JobStatus, expected_status: str
) -> None:
    """Each lifecycle event maps to a typed ``job_state_changed`` frame on the right session."""
    bus = EventBus()
    session = _make_session(dashboard_id="alpha")
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()
    job = _make_remote_job(status=status_field)

    payload: JobLifecycleData = {"job": job}
    bus.fire(event_type, payload)
    await _drain_background(controller)

    session.send_app_frame.assert_awaited_once()
    frame = session.send_app_frame.call_args.args[0]
    assert frame["type"] == "job_state_changed"
    assert frame["job_id"] == "wire-job"  # offloader's submit-tagged id
    assert frame["status"] == expected_status
    assert frame["error_message"] == ""


@pytest.mark.asyncio
async def test_failed_event_carries_error_message() -> None:
    """``failed`` carries ``FirmwareJob.error`` on the wire so the offloader can surface it."""
    bus = EventBus()
    session = _make_session()
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()
    job = _make_remote_job(status=JobStatus.FAILED, error="compile failed: bad pin")

    payload: JobLifecycleData = {"job": job}
    bus.fire(EventType.JOB_FAILED, payload)
    await _drain_background(controller)

    frame = session.send_app_frame.call_args.args[0]
    assert frame["status"] == "failed"
    assert frame["error_message"] == "compile failed: bad pin"


@pytest.mark.asyncio
async def test_local_job_does_not_fan_out() -> None:
    """A local-only job (``remote_peer=""``) is skipped at the listener."""
    bus = EventBus()
    session = _make_session()
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()
    local_job = FirmwareJob(
        job_id="local-only",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
    )

    bus.fire(EventType.JOB_STARTED, {"job": local_job})
    await _drain_background(controller)

    session.send_app_frame.assert_not_called()


@pytest.mark.asyncio
async def test_lifecycle_skips_when_session_gone() -> None:
    """A fan-out for a peer whose session has unregistered is skipped silently."""
    bus = EventBus()
    # No session under the "alpha" key — the offloader disconnected
    # mid-build.
    controller = _make_controller(bus=bus, sessions={})
    fanout = JobFanout(controller)
    fanout.start()
    job = _make_remote_job(status=JobStatus.RUNNING)

    bus.fire(EventType.JOB_STARTED, {"job": job})
    await _drain_background(controller)
    # No background task should have been queued — the lookup
    # returned None before reaching the send.
    assert controller.background_tasks == []


@pytest.mark.asyncio
async def test_job_queued_does_not_fan_out() -> None:
    """``JOB_QUEUED`` is intentionally not forwarded — the ack already signals queued.

    Pins the documented contract: the ``submit_job_ack``
    response carries the queued signal implicitly, and a
    redundant ``job_state_changed{queued}`` frame would race
    the ack and add wire noise without adding signal.
    """
    bus = EventBus()
    session = _make_session()
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()
    job = _make_remote_job(status=JobStatus.QUEUED)

    bus.fire(EventType.JOB_QUEUED, {"job": job})
    await _drain_background(controller)

    session.send_app_frame.assert_not_called()


# ---------------------------------------------------------------------------
# JOB_OUTPUT fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_output_fans_out_as_job_output_frame() -> None:
    """``JOB_OUTPUT`` for a remote-peer job sends a ``job_output`` frame."""
    bus = EventBus()
    session = _make_session(dashboard_id="alpha")
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    job = _make_remote_job(job_id="local-1", status=JobStatus.RUNNING)
    controller._db.firmware._jobs["local-1"] = job
    fanout = JobFanout(controller)
    fanout.start()

    payload: JobOutputData = {"job_id": "local-1", "line": "Compiling .pioenvs/...\n"}
    bus.fire(EventType.JOB_OUTPUT, payload)
    await _drain_background(controller)

    session.send_app_frame.assert_awaited_once()
    frame = session.send_app_frame.call_args.args[0]
    assert frame["type"] == "job_output"
    assert frame["job_id"] == "wire-job"
    assert frame["stream"] == "stdout"
    assert frame["line"] == "Compiling .pioenvs/...\n"


@pytest.mark.asyncio
async def test_job_output_skips_local_job() -> None:
    """``JOB_OUTPUT`` for a local job (no ``remote_peer``) is skipped."""
    bus = EventBus()
    session = _make_session()
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    local_job = FirmwareJob(
        job_id="local-only",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
    )
    controller._db.firmware._jobs["local-only"] = local_job
    fanout = JobFanout(controller)
    fanout.start()

    bus.fire(EventType.JOB_OUTPUT, {"job_id": "local-only", "line": "x"})
    await _drain_background(controller)

    session.send_app_frame.assert_not_called()


@pytest.mark.asyncio
async def test_job_output_skips_when_firmware_unavailable() -> None:
    """``JOB_OUTPUT`` early-returns cleanly when the firmware controller is None."""
    bus = EventBus()
    controller = _make_controller(bus=bus, sessions={})
    controller._db.firmware = None
    fanout = JobFanout(controller)
    fanout.start()

    bus.fire(EventType.JOB_OUTPUT, {"job_id": "any", "line": "x"})
    await _drain_background(controller)

    assert controller.background_tasks == []


@pytest.mark.asyncio
async def test_job_output_skips_when_session_gone() -> None:
    """``JOB_OUTPUT`` for a remote-peer job whose session unregistered is silently dropped."""
    bus = EventBus()
    controller = _make_controller(bus=bus, sessions={})  # no session
    job = _make_remote_job(job_id="local-1", status=JobStatus.RUNNING)
    controller._db.firmware._jobs["local-1"] = job
    fanout = JobFanout(controller)
    fanout.start()

    bus.fire(EventType.JOB_OUTPUT, {"job_id": "local-1", "line": "x"})
    await _drain_background(controller)

    assert controller.background_tasks == []


@pytest.mark.asyncio
async def test_job_output_skips_unknown_job_id() -> None:
    """``JOB_OUTPUT`` for a job_id not in the firmware registry is silently dropped."""
    bus = EventBus()
    session = _make_session()
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()

    bus.fire(EventType.JOB_OUTPUT, {"job_id": "ghost", "line": "x"})
    await _drain_background(controller)

    session.send_app_frame.assert_not_called()


# ---------------------------------------------------------------------------
# Lifecycle: stop + send-failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_detaches_listeners() -> None:
    """``stop()`` removes every listener registered by ``start()``.

    A subsequent ``JOB_STARTED`` fire after ``stop()`` should
    not reach the fan-out callback — proves the
    unsubscribe handles were collected and walked.
    """
    bus = EventBus()
    session = _make_session()
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()
    fanout.stop()

    bus.fire(EventType.JOB_STARTED, {"job": _make_remote_job(status=JobStatus.RUNNING)})
    await _drain_background(controller)

    session.send_app_frame.assert_not_called()


@pytest.mark.asyncio
async def test_send_app_frame_failure_is_swallowed() -> None:
    """A ``send_app_frame`` raise doesn't propagate out of the fan-out task."""
    bus = EventBus()
    session = _make_session()
    session.send_app_frame = AsyncMock(side_effect=RuntimeError("transport gone"))
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()

    bus.fire(EventType.JOB_STARTED, {"job": _make_remote_job(status=JobStatus.RUNNING)})
    # Drain doesn't raise — the helper logs at debug + returns.
    await _drain_background(controller)


def test_start_no_op_without_bus() -> None:
    """``start()`` is a no-op when the controller has no bus wired.

    The receiver's ``RemoteBuildController.start`` constructs
    the fan-out only when the firmware controller is present;
    bus presence is also a prerequisite (events flow through
    it). The guard here keeps the helper safe for tests that
    instantiate a stripped-down controller.
    """
    controller = _make_controller(bus=None)  # type: ignore[arg-type]
    controller._db.bus = None
    fanout = JobFanout(controller)
    fanout.start()
    # No listeners attached, ``stop`` walks an empty list cleanly.
    fanout.stop()
