"""Tests for ``firmware.follow_job`` ordering and trim-during-follow race.

Pin down two contracts:

1. Snapshot + subscribe are synchronous-adjacent so the streaming
   loop's appends can't slip between them. Without this, a line
   appended during the history-send awaits would fire a
   ``JOB_OUTPUT`` event with no subscriber attached for this
   follower — silently dropped. The in-flight output cap exacerbates
   the gap by reassigning ``job.output`` to a new list, making the
   old reference (still being iterated) blind to post-trim appends.
2. Live events fired during the history send still get delivered,
   in order, after the history finishes. Out-of-order delivery
   would surface in the dialog as "live tail comes back in time" —
   confusing, and breaks every "did line A appear before line B?"
   debug assumption.
"""

from __future__ import annotations

import asyncio
from typing import Any

from esphome_device_builder.controllers.firmware.constants import _MAX_OUTPUT_LINES_INFLIGHT
from esphome_device_builder.controllers.firmware.persistence import _write_job_sidecar
from esphome_device_builder.models import EventType, FirmwareJob, JobStatus, JobType, StreamEvent

from ...conftest import FakeWebSocketClient
from .conftest import FirmwareControllerFactory, make_follow_race_controller


async def test_terminal_job_replays_full_history_and_returns(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A finished job's history is replayed from its sidecar, followed by ``result``."""
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
        output=[],
        exit_code=0,
    )
    # Terminal output lives on disk (flushed + dropped from RAM at the
    # terminal transition); seed the sidecar as ``persist_jobs`` would.
    # Off the loop — the writer resolves ``CORE.data_dir`` (stats).
    await asyncio.to_thread(_write_job_sidecar, "abc", ["line a\n", "line b\n", "line c\n"])
    controller = make_follow_race_controller(firmware_controller_factory, job)
    client = FakeWebSocketClient(yield_per_event=True)

    await controller.follow_job(job_id="abc", client=client, message_id="m1")

    output_events = [(StreamEvent.OUTPUT, d) for d in client.events_for(StreamEvent.OUTPUT)]
    result_events = [(StreamEvent.RESULT, d) for d in client.events_for(StreamEvent.RESULT)]
    assert [d for _e, d in output_events] == ["line a\n", "line b\n", "line c\n"]
    assert len(result_events) == 1
    assert result_events[0][1] == {
        "status": "completed",
        "exit_code": 0,
        "error": None,
        "is_deferred_install": False,
    }


async def test_history_lines_arrive_before_live_lines_in_order(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Live events fired during the history send arrive after history.

    Drives the streaming loop concurrently with the follow_job
    history send: the test fires ``JOB_OUTPUT`` events from another
    task while ``follow_job`` is still iterating its snapshot. The
    snapshot+subscribe atomic block captures both halves without
    duplication and orders history before live.
    """
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=["history-1\n", "history-2\n"],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job)
    client = FakeWebSocketClient(yield_per_event=True)
    bus = controller._db.bus

    async def follower() -> None:
        await controller.follow_job(job_id="abc", client=client, message_id="m1")

    follow_task = asyncio.create_task(follower())
    # Yield once so follow_job runs through its synchronous setup
    # (snapshot + subscribe) and starts iterating the snapshot.
    await asyncio.sleep(0)

    # Now fire live events as if the streaming loop is running. The
    # listener queues them; follow_job's history send completes
    # first, then the drain loop delivers these in order.
    bus.fire(EventType.JOB_OUTPUT, {"job_id": "abc", "line": "live-1\n"})
    bus.fire(EventType.JOB_OUTPUT, {"job_id": "abc", "line": "live-2\n"})

    # Mark the job complete via the bus so the drain loop's terminal
    # sentinel fires and follow_job returns.
    job.status = JobStatus.COMPLETED
    job.exit_code = 0
    bus.fire(EventType.JOB_COMPLETED, {"job": job})

    await asyncio.wait_for(follow_task, timeout=2.0)

    output_lines = client.events_for(StreamEvent.OUTPUT)
    # Strict ordering: every history line strictly precedes every
    # live line, and within each group the original order is
    # preserved.
    assert output_lines == ["history-1\n", "history-2\n", "live-1\n", "live-2\n"]


async def test_live_events_for_other_jobs_are_filtered_out(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Listener ignores events for other ``job_id``s.

    Multiple jobs share the same bus; without filtering, the queue
    would fill with unrelated lines and the follower would deliver
    them as if they belonged to its own job.
    """
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=[],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job)
    client = FakeWebSocketClient(yield_per_event=True)
    bus = controller._db.bus

    async def follower() -> None:
        await controller.follow_job(job_id="abc", client=client, message_id="m1")

    follow_task = asyncio.create_task(follower())
    await asyncio.sleep(0)

    bus.fire(EventType.JOB_OUTPUT, {"job_id": "other", "line": "from other\n"})
    bus.fire(EventType.JOB_OUTPUT, {"job_id": "abc", "line": "from us\n"})
    bus.fire(EventType.JOB_OUTPUT, {"job_id": "other", "line": "from other 2\n"})

    job.status = JobStatus.COMPLETED
    job.exit_code = 0
    bus.fire(EventType.JOB_COMPLETED, {"job": job})

    await asyncio.wait_for(follow_task, timeout=2.0)

    output_lines = client.events_for(StreamEvent.OUTPUT)
    assert output_lines == ["from us\n"]


def _async_run(coro: Any) -> None:
    """Adapter so the snapshot-adjacency test can drive a sync race."""
    asyncio.run(coro)


async def test_streaming_loop_cannot_append_between_snapshot_and_subscribe(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Lines appended after follow_job starts appear via subscription, not history.

    Locks the contract that follow_job's history send and live drain
    don't overlap: a line fired before the follower's first await
    yields lands in the snapshot; a line fired after lands in the
    queue; never both, never neither. Without snapshot+subscribe
    atomicity the previous shape would either miss the line (it
    fires before the listener attaches) or duplicate it (it fires
    after the snapshot but is also caught by the listener).
    """
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=["pre-snapshot\n"],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job)
    client = FakeWebSocketClient(yield_per_event=True)
    bus = controller._db.bus

    async def follower() -> None:
        await controller.follow_job(job_id="abc", client=client, message_id="m1")

    follow_task = asyncio.create_task(follower())
    # Yield so the synchronous setup completes (snapshot + subscribe).
    await asyncio.sleep(0)

    # Fire after subscribe — listener captures.
    bus.fire(EventType.JOB_OUTPUT, {"job_id": "abc", "line": "post-snapshot\n"})

    job.status = JobStatus.COMPLETED
    job.exit_code = 0
    bus.fire(EventType.JOB_COMPLETED, {"job": job})

    await asyncio.wait_for(follow_task, timeout=2.0)

    output_lines = client.events_for(StreamEvent.OUTPUT)
    # Exactly one of each — no duplication of pre-snapshot, no
    # missing post-snapshot.
    assert output_lines == ["pre-snapshot\n", "post-snapshot\n"]


async def test_slow_follower_drops_lines_above_queue_cap(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Bounded queue caps memory: lines past the cap are dropped.

    Without this bound, a follower that stops draining (closed WS,
    backpressured client) would let the listener accumulate every
    fired line in memory forever — the in-flight ``job.output``
    cap on the build itself bounds one buffer, but each follower
    held a second unbounded one. Test parks the follower in
    ``send_event`` and fires a burst larger than the queue can
    hold; the producer stays unblocked because ``put_nowait``
    drops on full instead of awaiting drain capacity.
    """
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=[],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job)
    bus = controller._db.bus

    # Park send_event so the drain stays blocked on the very first
    # delivered line. With the drain parked, the queue can fill all
    # the way to its cap and start dropping.
    block = asyncio.Event()
    received: list[tuple[str, Any]] = []

    class BlockingClient:
        def register_stream(self, _mid: str, _task: Any) -> None: ...
        def unregister_stream(self, _mid: str) -> None: ...

        async def send_event(self, _mid: str, event: str, data: Any) -> None:
            received.append((event, data))
            await block.wait()

    follow_task = asyncio.create_task(
        controller.follow_job(job_id="abc", client=BlockingClient(), message_id="m1")
    )
    await asyncio.sleep(0)

    # Fire one line so the drain task picks it up and parks in
    # send_event. After this point all subsequent fires accumulate
    # in the queue.
    bus.fire(EventType.JOB_OUTPUT, {"job_id": "abc", "line": "first\n"})
    await asyncio.sleep(0)
    assert received == [(StreamEvent.OUTPUT, "first\n")]

    # Fire well past the cap. Without the bound this would grow the
    # queue unboundedly; with it the queue caps at maxsize and the
    # excess fires no-op at put_nowait.
    burst_size = _MAX_OUTPUT_LINES_INFLIGHT + 500
    for i in range(burst_size):
        bus.fire(EventType.JOB_OUTPUT, {"job_id": "abc", "line": f"l{i}\n"})

    # Unblock the drain so we can shut down cleanly.
    block.set()
    job.status = JobStatus.COMPLETED
    job.exit_code = 0
    bus.fire(EventType.JOB_COMPLETED, {"job": job})

    await asyncio.wait_for(follow_task, timeout=2.0)

    output_count = sum(1 for (e, _) in received if e == StreamEvent.OUTPUT)
    # The follower must have lost some lines — the bound was the
    # whole point. The first line + at most the queue cap delivered.
    assert output_count <= 1 + _MAX_OUTPUT_LINES_INFLIGHT
    # And a result still arrives even with output dropped.
    result_events = [d for (e, d) in received if e == StreamEvent.RESULT]
    assert len(result_events) == 1


async def test_terminal_sentinel_evicts_to_unblock_drain_when_queue_full(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Terminal result + sentinel still land when the queue is full.

    The output put_nowait drops on full, but the result + sentinel
    use ``_put_evicting`` — they MUST reach the drain loop or the
    follower parks on ``queue.get`` forever after the build
    finishes. Without eviction a slow follower could pile up
    output, reach cap, then a JOB_COMPLETED would silently
    no-op and the follower would hang.
    """
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=[],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job)
    bus = controller._db.bus

    block = asyncio.Event()
    received: list[tuple[str, Any]] = []

    class BlockingClient:
        def register_stream(self, _mid: str, _task: Any) -> None: ...
        def unregister_stream(self, _mid: str) -> None: ...

        async def send_event(self, _mid: str, event: str, data: Any) -> None:
            received.append((event, data))
            # Only block on output; let result/sentinel through so
            # the test can observe completion.
            if event == StreamEvent.OUTPUT:
                await block.wait()

    follow_task = asyncio.create_task(
        controller.follow_job(job_id="abc", client=BlockingClient(), message_id="m1")
    )
    await asyncio.sleep(0)

    # Park the drain in send_event with a single line.
    bus.fire(EventType.JOB_OUTPUT, {"job_id": "abc", "line": "first\n"})
    await asyncio.sleep(0)

    # Fill the queue to capacity.
    for i in range(_MAX_OUTPUT_LINES_INFLIGHT):
        bus.fire(EventType.JOB_OUTPUT, {"job_id": "abc", "line": f"l{i}\n"})

    # Now mark the job complete. The terminal handler uses
    # ``_put_evicting`` to push (result, ...) and the sentinel —
    # both must displace older items rather than no-op, so the
    # drain eventually breaks.
    job.status = JobStatus.COMPLETED
    job.exit_code = 0
    bus.fire(EventType.JOB_COMPLETED, {"job": job})

    # Unblock the drain so it can finish processing.
    block.set()

    await asyncio.wait_for(follow_task, timeout=2.0)

    result_events = [d for (e, d) in received if e == StreamEvent.RESULT]
    assert len(result_events) == 1
    assert result_events[0]["status"] == "completed"


async def test_cancelled_terminal_event_returns_with_status(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``JOB_CANCELLED`` ends the follow with a ``cancelled`` result.

    Mirrors the completed/failed paths but exercises the third
    entry in ``TERMINAL_JOB_EVENTS``. Without coverage here a
    listener change that drops ``JOB_CANCELLED`` from the
    subscribed set would silently leave followers parked on the
    queue forever — the build's own runner has stopped firing
    output, so the drain loop never sees a sentinel.
    """
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=["pre-cancel\n"],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job)
    client = FakeWebSocketClient(yield_per_event=True)
    bus = controller._db.bus

    async def follower() -> None:
        await controller.follow_job(job_id="abc", client=client, message_id="m1")

    follow_task = asyncio.create_task(follower())
    await asyncio.sleep(0)

    job.status = JobStatus.CANCELLED
    bus.fire(EventType.JOB_CANCELLED, {"job": job})

    await asyncio.wait_for(follow_task, timeout=2.0)

    output_lines = client.events_for(StreamEvent.OUTPUT)
    result_events = client.events_for(StreamEvent.RESULT)
    assert output_lines == ["pre-cancel\n"]
    assert len(result_events) == 1
    assert result_events[0]["status"] == "cancelled"


async def test_failed_terminal_event_carries_job_error_text(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``JOB_FAILED`` RESULT payload carries the human-readable ``error``.

    The install dialog's red error banner falls back to a generic
    "Install failed." copy when no specific text is available; without
    the ``error`` field on the wire it can't distinguish a C++ build
    failure (where clean / reset is the right hint) from a session-lost
    failure (where neither helps and the operator just needs to retry).
    Pin the wire shape so a future refactor that drops the field
    surfaces here.
    """
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=["compiling main.cpp\n"],
    )
    controller = make_follow_race_controller(firmware_controller_factory, job)
    client = FakeWebSocketClient(yield_per_event=True)
    bus = controller._db.bus

    async def follower() -> None:
        await controller.follow_job(job_id="abc", client=client, message_id="m1")

    follow_task = asyncio.create_task(follower())
    await asyncio.sleep(0)

    job.status = JobStatus.FAILED
    job.error = "remote build: peer-link session lost (transport_error: …)"
    job.exit_code = None
    bus.fire(EventType.JOB_FAILED, {"job": job})

    await asyncio.wait_for(follow_task, timeout=2.0)

    result_events = client.events_for(StreamEvent.RESULT)
    assert len(result_events) == 1
    assert result_events[0] == {
        "status": "failed",
        "exit_code": None,
        "error": "remote build: peer-link session lost (transport_error: …)",
        "is_deferred_install": False,
    }


async def test_send_initial_replays_error_for_already_terminal_failed_job(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A follower that connects after the job already failed reads the error.

    Covers the ``_send_initial`` branch that fires when ``follow_job``
    is called on a job whose terminal-event already landed before the
    subscription. The replay must include ``error`` so the late
    follower's banner still gets the specific failure text.
    """
    job = FirmwareJob(
        job_id="abc",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.FAILED,
        output=["compile error: …\n"],
        exit_code=1,
        error="Process exited 1",
    )
    controller = make_follow_race_controller(firmware_controller_factory, job)
    client = FakeWebSocketClient(yield_per_event=True)

    await controller.follow_job(job_id="abc", client=client, message_id="m1")

    result_events = client.events_for(StreamEvent.RESULT)
    assert len(result_events) == 1
    assert result_events[0] == {
        "status": "failed",
        "exit_code": 1,
        "error": "Process exited 1",
        "is_deferred_install": False,
    }
