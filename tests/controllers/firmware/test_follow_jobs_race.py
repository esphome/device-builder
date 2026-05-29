"""Tests for ``firmware.follow_jobs`` snapshot+live ordering.

The plural ``follow_jobs`` (the all-jobs panel feed) had the same
snapshot/live race the singular ``follow_job`` was fixed for: it
sent the initial snapshot first and only attached listeners
afterwards, so a ``JOB_*`` event firing during the snapshot replay
was silently lost. The fix is the same — route through
``stream_events`` so listeners attach before ``send_initial`` is
awaited.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from esphome_device_builder.models import EventType, FirmwareJob, JobStatus, JobType, StreamEvent

from ...conftest import FakeWebSocketClient
from .conftest import FirmwareControllerFactory, make_follow_race_controller


async def test_follow_jobs_replays_snapshot_then_live_events_in_order(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A ``JOB_*`` event during the snapshot replay still reaches the client.

    Without the fix the snapshot loop awaited each ``send_event``,
    yielding the loop, and any ``JOB_QUEUED`` / ``JOB_COMPLETED``
    fired during those awaits had no listener and was lost. With
    listeners attached before ``send_initial`` runs, those events
    queue and arrive after the snapshot.
    """
    job_a = FirmwareJob(
        job_id="a",
        configuration="a.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
        output=[],
    )
    job_b = FirmwareJob(
        job_id="b",
        configuration="b.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=[],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job_a, job_b)
    client = FakeWebSocketClient(yield_per_event=True)
    bus = controller._db.bus

    follow_task = asyncio.create_task(controller.follow_jobs(client=client, message_id="m1"))
    # Yield once so the helper finishes its synchronous setup
    # (listeners attached) and starts awaiting in send_initial.
    await asyncio.sleep(0)

    # Fire a JOB_QUEUED while the snapshot replay is in flight. The
    # listener queues it; the helper drains it after the snapshot
    # finishes.
    new_job = FirmwareJob(
        job_id="c",
        configuration="c.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.QUEUED,
        output=[],
    )
    bus.fire(EventType.JOB_QUEUED, {"job": new_job})

    # Yield enough that the snapshot + the live event get drained.
    for _ in range(20):
        await asyncio.sleep(0)
        if any(e == EventType.JOB_QUEUED for (_mid, e, _d) in client.events):
            break

    follow_task.cancel()
    with suppress(asyncio.CancelledError):
        await follow_task

    snapshot_events = client.events_for(StreamEvent.SNAPSHOT)
    queued_events = client.events_for(EventType.JOB_QUEUED)
    # Both jobs are in the snapshot (sorted by created_at).
    assert {s["job_id"] for s in snapshot_events} == {"a", "b"}
    # The live JOB_QUEUED arrives after the snapshot.
    assert len(queued_events) == 1
    assert queued_events[0]["job_id"] == "c"
    # Strict ordering: every snapshot event precedes the live event.
    snapshot_indices = client.indices_for(StreamEvent.SNAPSHOT)
    queued_index = client.first_index_for(EventType.JOB_QUEUED)
    assert max(snapshot_indices) < queued_index


async def test_follow_jobs_snapshot_does_not_duplicate_with_concurrent_mutation(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A running job mutating mid-snapshot delivers its line once, via the live event.

    Snapshots carry no ``output`` (terminal logs come from the
    sidecar via ``follow_job``; a running job's lines arrive as
    live ``JOB_OUTPUT`` events), so a mid-snapshot mutation can't
    leak into the snapshot path. This pins that the line lands
    exactly once — through the live event, never duplicated into
    the snapshot — with the multi-job shape: snapshot[0] parks the
    helper, the test mutates ``job_b.output`` and fires the
    matching ``JOB_OUTPUT``, then unblocks and checks the snapshot
    omits output while the live event is delivered once.
    """
    job_a = FirmwareJob(
        job_id="a",
        configuration="a.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=["a-line\n"],
    )
    job_b = FirmwareJob(
        job_id="b",
        configuration="b.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=["b-pre-snapshot\n"],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job_a, job_b)
    bus = controller._db.bus

    block = asyncio.Event()
    received: list[tuple[str, Any]] = []

    class GatedClient:
        async def send_event(self, _message_id: str, event: str, data: Any) -> None:
            received.append((event, data))
            # Park after delivering snapshot[0] so the test can
            # mutate job_b before the helper iterates to it.
            if event == StreamEvent.SNAPSHOT and data["job_id"] == "a":
                await block.wait()

    follow_task = asyncio.create_task(controller.follow_jobs(client=GatedClient(), message_id="m1"))
    # Yield until the helper has delivered snapshot[a] and is
    # parked on ``block.wait()``.
    for _ in range(20):
        await asyncio.sleep(0)
        if received:
            break
    assert received[0][0] == StreamEvent.SNAPSHOT
    assert received[0][1]["job_id"] == "a"

    # Mutate job_b AND fire the matching JOB_OUTPUT — the same
    # interleaving the runner produces between snapshot iterations.
    job_b.output.append("b-mid-snapshot\n")
    bus.fire(EventType.JOB_OUTPUT, {"job_id": "b", "line": "b-mid-snapshot\n"})

    # Release the helper and let it iterate to snapshot[b] then
    # drain the queued live event.
    block.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if any(e == EventType.JOB_OUTPUT for (e, _d) in received):
            break

    follow_task.cancel()
    with suppress(asyncio.CancelledError):
        await follow_task

    snapshots = {d["job_id"]: d for (e, d) in received if e == StreamEvent.SNAPSHOT}
    job_outputs = [d for (e, d) in received if e == EventType.JOB_OUTPUT]

    # Snapshots omit output entirely, so the mid-snapshot mutation
    # can't leak into snapshot[b].
    assert "output" not in snapshots["b"]
    # The mid-snapshot line lands exactly once via the live
    # event — not duplicated through the snapshot path.
    assert job_outputs == [{"job_id": "b", "line": "b-mid-snapshot\n"}]


async def test_follow_jobs_unsubscribes_on_cancellation(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Cancelling the WS task releases every listener.

    Three event types are subscribed (lifecycle, output, progress);
    a leak here would silently grow the listener set on every
    reconnect.
    """
    controller = make_follow_race_controller(firmware_controller_factory)
    bus = controller._db.bus
    client = FakeWebSocketClient(yield_per_event=True)

    follow_task = asyncio.create_task(controller.follow_jobs(client=client, message_id="m1"))
    await asyncio.sleep(0)

    listener_count_before = sum(len(bus._listeners.get(et, ())) for et in EventType)
    assert listener_count_before > 0

    follow_task.cancel()
    with suppress(asyncio.CancelledError):
        await follow_task

    listener_count_after = sum(len(bus._listeners.get(et, ())) for et in EventType)
    assert listener_count_after == 0


# ---------------------------------------------------------------------------
# _handle_event branch coverage
# ---------------------------------------------------------------------------


async def _drain_until(client: FakeWebSocketClient, predicate: Any, attempts: int = 20) -> None:
    """Yield the event loop until *predicate(client)* is truthy or *attempts* runs out."""
    for _ in range(attempts):
        await asyncio.sleep(0)
        if predicate(client):
            return


async def test_follow_jobs_forwards_job_progress_events(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A ``JOB_PROGRESS`` event lands as a ``job_progress`` push (not a lifecycle)."""
    controller = make_follow_race_controller(firmware_controller_factory)
    bus = controller._db.bus
    client = FakeWebSocketClient(yield_per_event=True)

    follow_task = asyncio.create_task(controller.follow_jobs(client=client, message_id="m1"))
    await asyncio.sleep(0)

    progress = {"job_id": "a", "stage": "compile", "percent": 42}
    bus.fire(EventType.JOB_PROGRESS, progress)

    await _drain_until(client, lambda c: any(e == "job_progress" for (_m, e, _d) in c.events))

    follow_task.cancel()
    with suppress(asyncio.CancelledError):
        await follow_task

    progress_events = client.events_for("job_progress")
    assert progress_events == [progress]


async def test_follow_jobs_drops_lifecycle_event_with_no_job_payload(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A lifecycle event missing the ``job`` key is silently ignored.

    The runner always tags lifecycle bus events with ``{"job": <FirmwareJob>}``,
    but the handler defends against a malformed payload by skipping it
    instead of crashing the stream — guard the early-return path here.
    """
    controller = make_follow_race_controller(firmware_controller_factory)
    bus = controller._db.bus
    client = FakeWebSocketClient(yield_per_event=True)

    follow_task = asyncio.create_task(controller.follow_jobs(client=client, message_id="m1"))
    await asyncio.sleep(0)

    # Bare payload — no ``job`` key. The handler must early-return
    # without raising and without pushing anything.
    bus.fire(EventType.JOB_QUEUED, {"unrelated": "noise"})

    # And then a normal event so we have a synchronisation point —
    # if the handler had crashed, the listener would be torn down
    # and this second event would never reach the client.
    bus.fire(EventType.JOB_PROGRESS, {"job_id": "a"})
    await _drain_until(client, lambda c: any(e == "job_progress" for (_m, e, _d) in c.events))

    follow_task.cancel()
    with suppress(asyncio.CancelledError):
        await follow_task

    assert client.events_for("job_queued") == []
    assert client.events_for("job_progress") == [{"job_id": "a"}]


async def test_follow_jobs_lifecycle_payload_passthrough_for_dict_job(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A ``job`` payload that's already a dict is forwarded verbatim.

    Production firmware events carry a ``FirmwareJob`` instance and the
    handler calls ``to_dict()`` on it. Replays from persisted history,
    however, can hand back a plain dict — the handler's ``hasattr``
    check falls through and uses the dict as-is.
    """
    controller = make_follow_race_controller(firmware_controller_factory)
    bus = controller._db.bus
    client = FakeWebSocketClient(yield_per_event=True)

    follow_task = asyncio.create_task(controller.follow_jobs(client=client, message_id="m1"))
    await asyncio.sleep(0)

    raw = {"job_id": "z", "status": "completed"}
    bus.fire(EventType.JOB_COMPLETED, {"job": raw})
    await _drain_until(client, lambda c: any(e == "job_completed" for (_m, e, _d) in c.events))

    follow_task.cancel()
    with suppress(asyncio.CancelledError):
        await follow_task

    completed = client.events_for("job_completed")
    assert completed == [raw]
