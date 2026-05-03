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
from unittest.mock import MagicMock

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import EventType, FirmwareJob, JobStatus, JobType

from ...conftest import FakeWebSocketClient


def _make_controller(jobs: list[FirmwareJob]) -> FirmwareController:
    controller = FirmwareController.__new__(FirmwareController)
    controller._jobs = {job.job_id: job for job in jobs}
    db = MagicMock()
    db.bus = EventBus()
    controller._db = db
    return controller


async def test_follow_jobs_replays_snapshot_then_live_events_in_order() -> None:
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
    controller = _make_controller([job_a, job_b])
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
        if any(e == "job_queued" for (_mid, e, _d) in client.events):
            break

    follow_task.cancel()
    with suppress(asyncio.CancelledError):
        await follow_task

    snapshot_events = client.events_for("snapshot")
    queued_events = client.events_for("job_queued")
    # Both jobs are in the snapshot (sorted by created_at).
    assert {s["job_id"] for s in snapshot_events} == {"a", "b"}
    # The live JOB_QUEUED arrives after the snapshot.
    assert len(queued_events) == 1
    assert queued_events[0]["job_id"] == "c"
    # Strict ordering: every snapshot event precedes the live event.
    snapshot_indices = [i for i, (_mid, e, _d) in enumerate(client.events) if e == "snapshot"]
    queued_index = next(i for i, (_mid, e, _d) in enumerate(client.events) if e == "job_queued")
    assert max(snapshot_indices) < queued_index


async def test_follow_jobs_snapshot_does_not_duplicate_with_concurrent_mutation() -> None:
    """A running job mutating mid-snapshot doesn't appear twice.

    The earlier shape captured ``FirmwareJob`` objects synchronously
    and called ``to_dict()`` later inside ``send_initial``. With
    multiple jobs that's racy: between iterations of the snapshot
    loop, each ``await send_event(...)`` yields the loop, the
    runner runs and can mutate the *next* job's ``output``, then
    the next iteration's ``to_dict()`` captures the mutated state.
    The matching live ``JOB_OUTPUT`` is also delivered by the
    listener — so the client sees the same line twice (once in
    snapshot, once in the drain).

    The fix is to dict-freeze the snapshot synchronously before
    ``stream_events`` attaches listeners (no awaits between
    freeze and attach). This test pins it with the
    multi-job shape: snapshot[0] parks the helper in
    ``send_event``, the test mutates ``job_b.output`` and fires
    the matching ``JOB_OUTPUT``, then unblocks the helper and
    checks that snapshot[1] has *only* the pre-mutation output
    while the live event is delivered exactly once.
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
    controller = _make_controller([job_a, job_b])
    bus = controller._db.bus

    block = asyncio.Event()
    received: list[tuple[str, Any]] = []

    class GatedClient:
        async def send_event(self, _message_id: str, event: str, data: Any) -> None:
            received.append((event, data))
            # Park after delivering snapshot[0] so the test can
            # mutate job_b before the helper iterates to it.
            if event == "snapshot" and data["job_id"] == "a":
                await block.wait()

    follow_task = asyncio.create_task(controller.follow_jobs(client=GatedClient(), message_id="m1"))
    # Yield until the helper has delivered snapshot[a] and is
    # parked on ``block.wait()``.
    for _ in range(20):
        await asyncio.sleep(0)
        if received:
            break
    assert received[0][0] == "snapshot"
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
        if any(e == "job_output" for (e, _d) in received):
            break

    follow_task.cancel()
    with suppress(asyncio.CancelledError):
        await follow_task

    snapshots = {d["job_id"]: d for (e, d) in received if e == "snapshot"}
    job_outputs = [d for (e, d) in received if e == "job_output"]

    # snapshot[b] was frozen synchronously up front (before any
    # await), so the mid-snapshot mutation isn't reflected.
    assert snapshots["b"]["output"] == ["b-pre-snapshot\n"]
    # The mid-snapshot line lands exactly once via the live
    # event — not duplicated through the snapshot path.
    assert job_outputs == [{"job_id": "b", "line": "b-mid-snapshot\n"}]


async def test_follow_jobs_unsubscribes_on_cancellation() -> None:
    """Cancelling the WS task releases every listener.

    Three event types are subscribed (lifecycle, output, progress);
    a leak here would silently grow the listener set on every
    reconnect.
    """
    controller = _make_controller([])
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
