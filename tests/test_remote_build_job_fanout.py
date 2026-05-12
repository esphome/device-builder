"""
Tests for the receiver-side ``JOB_*`` event fan-out to peer-link frames.

Pairs with the receiver-side ``submit_job`` accept path: the
accept path queues a :class:`FirmwareJob` with ``remote_peer`` +
``remote_job_id`` set; this module's :class:`JobFanout` subscribes
to firmware ``JOB_*`` events and forwards remote-peer jobs as
``job_state_changed`` / ``job_output`` frames over the submitting
peer-link session.

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

from esphome_device_builder.controllers.remote_build.job_fanout import JobFanout
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
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
    """Stub :class:`ReceiverController` with the attributes ``JobFanout`` reads.

    Three load-bearing fields:

    * ``_db.bus`` — fan-out attaches its listeners here.
    * ``_peer_link_sessions`` — per-event session lookup keyed
      on ``FirmwareJob.remote_peer``.
    * ``_db.create_background_task`` — captures each
      ``send_app_frame`` coroutine into ``background_tasks``
      so :func:`_drain_background` can run them deterministically
      (no timing-based polling).
    """
    background_tasks: list[Any] = []

    def _create_background_task(coro: Any) -> Any:
        background_tasks.append(coro)
        return MagicMock()

    db = MagicMock()
    db.bus = bus
    db.create_background_task = _create_background_task

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


def _seed_via_queued(bus: EventBus, job: FirmwareJob) -> None:
    """Fire ``JOB_QUEUED`` so the fan-out caches the remote-peer correlation.

    Production fires ``JOB_QUEUED`` from
    :meth:`FirmwareController._enqueue` before any subsequent
    lifecycle / output event for the same job, so tests
    exercising those later events need to seed the fan-out's
    cache the same way the real flow would.
    """
    bus.fire(EventType.JOB_QUEUED, {"job": job})


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
    _seed_via_queued(bus, job)
    await _drain_background(controller)  # flush the queued frame's fan-out
    session.send_app_frame.reset_mock()

    bus.fire(event_type, {"job": job})
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
    _seed_via_queued(bus, job)
    await _drain_background(controller)  # flush the queued frame's fan-out
    session.send_app_frame.reset_mock()

    bus.fire(EventType.JOB_FAILED, {"job": job})
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
    # mid-build (after the fan-out cached its correlation tuple
    # via JOB_QUEUED).
    controller = _make_controller(bus=bus, sessions={})
    fanout = JobFanout(controller)
    fanout.start()
    job = _make_remote_job(status=JobStatus.RUNNING)
    _seed_via_queued(bus, job)

    bus.fire(EventType.JOB_STARTED, {"job": job})
    await _drain_background(controller)
    # No background task should have been queued — the session
    # lookup returned None before reaching the send.
    assert controller.background_tasks == []


@pytest.mark.asyncio
async def test_job_queued_caches_correlation_and_fans_out_queued_frame() -> None:
    """``JOB_QUEUED`` populates the cache AND fans out a ``queued`` wire frame.

    The ``queued`` fan-out is what drives the offloader's
    "waiting in line" screen when the receiver is busy with
    another offloader's job.
    """
    bus = EventBus()
    session = _make_session(dashboard_id="alpha")
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()
    job = _make_remote_job(status=JobStatus.QUEUED)

    bus.fire(EventType.JOB_QUEUED, {"job": job})
    await _drain_background(controller)

    session.send_app_frame.assert_awaited_once()
    frame = session.send_app_frame.call_args.args[0]
    assert frame["type"] == "job_state_changed"
    assert frame["job_id"] == "wire-job"
    assert frame["status"] == "queued"
    assert frame["error_message"] == ""
    # Cache populated — the JOB_STARTED that lands next will
    # find its correlation tuple.
    assert fanout._remote_jobs == {job.job_id: ("alpha", "wire-job")}


@pytest.mark.asyncio
async def test_queued_with_missing_remote_job_id_logs_and_skips_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote-peer job missing ``remote_job_id`` logs at debug and isn't cached.

    Pins the upgrade-shape behaviour: a persisted ``FirmwareJob``
    from before ``remote_job_id`` existed (or a future call
    site forgetting to thread the field through
    ``_create_job``) carries ``remote_peer`` set but
    ``remote_job_id`` empty. Without the diagnostic log here
    the silent cache miss would mask the missing correlation
    on every subsequent lifecycle / output event for the job.

    Captures log calls via a monkey-patched ``_LOGGER.debug``
    rather than ``caplog.at_level("DEBUG")`` because the latter
    is flaky on the pytest-asyncio + Python 3.14 combo (the
    capture handler is set up around the test body but not
    always around the bus listener fire that runs synchronously
    inside ``bus.fire``).
    """
    bus = EventBus()
    session = _make_session()
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()
    job = _make_remote_job(remote_job_id="")
    debug_calls: list[str] = []
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.job_fanout._LOGGER.debug",
        lambda fmt, *args, **kwargs: debug_calls.append(fmt % args if args else fmt),
    )

    bus.fire(EventType.JOB_QUEUED, {"job": job})

    assert job.job_id not in fanout._remote_jobs
    assert any("missing remote_job_id" in msg for msg in debug_calls)


@pytest.mark.asyncio
async def test_terminal_event_drops_cache_entry() -> None:
    """A terminal event (completed / failed / cancelled) drops the cache entry.

    Pins the leak-prevention contract: the firmware controller
    retains ``FirmwareJob`` rows for post-mortem inspection, so
    relying on a "job removed" signal isn't an option. The
    fan-out tracks lifecycle directly and clears its own state
    when the job goes terminal.
    """
    bus = EventBus()
    session = _make_session()
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    fanout = JobFanout(controller)
    fanout.start()
    job = _make_remote_job()
    _seed_via_queued(bus, job)
    assert job.job_id in fanout._remote_jobs

    bus.fire(EventType.JOB_COMPLETED, {"job": job})
    await _drain_background(controller)

    assert job.job_id not in fanout._remote_jobs


# ---------------------------------------------------------------------------
# JOB_OUTPUT fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_output_fans_out_as_job_output_frame() -> None:
    """``JOB_OUTPUT`` for a cached remote-peer job sends a ``job_output`` frame."""
    bus = EventBus()
    session = _make_session(dashboard_id="alpha")
    controller = _make_controller(bus=bus, sessions={"alpha": session})
    job = _make_remote_job(job_id="local-1", status=JobStatus.RUNNING)
    fanout = JobFanout(controller)
    fanout.start()
    _seed_via_queued(bus, job)
    await _drain_background(controller)  # flush the queued frame's fan-out
    session.send_app_frame.reset_mock()

    bus.fire(EventType.JOB_OUTPUT, {"job_id": "local-1", "line": "Compiling .pioenvs/...\n"})
    await _drain_background(controller)

    session.send_app_frame.assert_awaited_once()
    frame = session.send_app_frame.call_args.args[0]
    assert frame["type"] == "job_output"
    assert frame["job_id"] == "wire-job"
    assert frame["stream"] == "stdout"
    assert frame["line"] == "Compiling .pioenvs/...\n"


@pytest.mark.asyncio
async def test_job_output_skips_local_job() -> None:
    """A local job's JOB_QUEUED never enters the cache, so its JOB_OUTPUT is skipped."""
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
    _seed_via_queued(bus, local_job)  # remote_peer empty → cache miss

    bus.fire(EventType.JOB_OUTPUT, {"job_id": "local-only", "line": "x"})
    await _drain_background(controller)

    session.send_app_frame.assert_not_called()
    assert local_job.job_id not in fanout._remote_jobs


@pytest.mark.asyncio
async def test_job_output_skips_when_session_gone() -> None:
    """``JOB_OUTPUT`` for a cached job whose session unregistered is silently dropped."""
    bus = EventBus()
    controller = _make_controller(bus=bus, sessions={})  # no session
    job = _make_remote_job(job_id="local-1", status=JobStatus.RUNNING)
    fanout = JobFanout(controller)
    fanout.start()
    _seed_via_queued(bus, job)

    bus.fire(EventType.JOB_OUTPUT, {"job_id": "local-1", "line": "x"})
    await _drain_background(controller)

    assert controller.background_tasks == []


@pytest.mark.asyncio
async def test_job_output_skips_unknown_job_id() -> None:
    """``JOB_OUTPUT`` for an unseen ``job_id`` (no JOB_QUEUED before) is silently dropped."""
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
    job = _make_remote_job(status=JobStatus.RUNNING)
    _seed_via_queued(bus, job)

    bus.fire(EventType.JOB_STARTED, {"job": job})
    # Drain doesn't raise — the helper logs at debug + returns.
    await _drain_background(controller)


def test_start_no_op_without_bus() -> None:
    """``start()`` is a no-op when the controller has no bus wired.

    The receiver's ``ReceiverController.start`` constructs
    the fan-out only when the firmware controller is present;
    bus presence is also a prerequisite (events flow through
    it). The guard here keeps the helper safe for tests that
    instantiate a stripped-down controller.
    """
    controller = _make_controller(bus=None)  # type: ignore[arg-type]
    controller.offloader._db.bus = None
    fanout = JobFanout(controller)
    fanout.start()
    # No listeners attached, ``stop`` walks an empty list cleanly.
    fanout.stop()


# ---------------------------------------------------------------------------
# Reverse-lookup for cancel_job
# ---------------------------------------------------------------------------


def test_resolve_firmware_job_id_returns_match() -> None:
    """Walks the cache and returns the receiver-local id on a match."""
    controller = _make_controller(bus=EventBus())
    fanout = JobFanout(controller)
    fanout._remote_jobs["fw-1"] = ("offloader-1", "remote-a")
    fanout._remote_jobs["fw-2"] = ("offloader-2", "remote-b")
    assert fanout.resolve_firmware_job_id("offloader-1", "remote-a") == "fw-1"
    assert fanout.resolve_firmware_job_id("offloader-2", "remote-b") == "fw-2"


def test_resolve_firmware_job_id_returns_none_on_miss() -> None:
    """A non-matching ``(remote_peer, remote_job_id)`` returns ``None``, doesn't raise."""
    controller = _make_controller(bus=EventBus())
    fanout = JobFanout(controller)
    fanout._remote_jobs["fw-1"] = ("offloader-1", "remote-a")
    assert fanout.resolve_firmware_job_id("offloader-1", "wrong-job") is None
    assert fanout.resolve_firmware_job_id("wrong-peer", "remote-a") is None
    assert fanout.resolve_firmware_job_id("totally", "unknown") is None


def test_resolve_firmware_job_id_empty_cache_returns_none() -> None:
    """An empty cache returns ``None`` for any lookup."""
    controller = _make_controller(bus=EventBus())
    fanout = JobFanout(controller)
    assert fanout.resolve_firmware_job_id("offloader-1", "remote-a") is None
