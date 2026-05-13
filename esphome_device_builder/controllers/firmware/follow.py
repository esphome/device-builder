"""Firmware-job WS streaming endpoints: follow_job + follow_jobs."""

from __future__ import annotations

from operator import attrgetter
from typing import TYPE_CHECKING, Any

from ...helpers.event_bus import StreamControls, stream_events
from ...models import (
    TERMINAL_JOB_EVENTS,
    TERMINAL_JOB_STATUSES,
    EventType,
    StreamEvent,
)

if TYPE_CHECKING:
    from ...helpers.event_bus import Event
    from .controller import FirmwareController


async def follow_job(
    controller: FirmwareController,
    *,
    job_id: str,
    client: Any = None,
    message_id: str = "",
) -> None:
    """
    Follow a job's output: send historical lines then stream new ones.

    Behaves like ``tail -f`` with history. If the job is already
    finished, sends all output and a final result event.

    Race-free against the streaming loop: ``stream_events``
    subscribes to ``JOB_OUTPUT`` *before* the snapshot is sent,
    so the streaming loop cannot append between the snapshot
    capture and the subscription. Without that ordering, the
    previous shape iterated ``job.output`` directly and only
    subscribed afterwards, which had two failure modes:

    1. Lines appended to ``job.output`` during the history send
       (each ``send_event`` await yields the loop) fired a
       ``JOB_OUTPUT`` event with no subscriber attached and were
       dropped for this follower.
    2. The in-flight cap's ``_trim_job_output`` reassigns
       ``job.output`` to a new list, so an iteration over the
       old list reference stops seeing post-trim appends — making
       the gap above strictly bigger after every cap-crossing.

    Both failure modes are closed by snapshotting *before*
    ``stream_events`` runs and replaying inside ``send_initial``
    — every line fired after that point queues through the
    listener and lands strictly after history.
    """
    job = controller._jobs.get(job_id)
    if not job:
        msg = f"Job not found: {job_id}"
        raise ValueError(msg)

    # Capture snapshot before stream_events attaches listeners.
    # The listener (attached inside stream_events) catches every
    # line fired after this point; nothing fires between the
    # snapshot and the subscribe because both happen in
    # synchronous-adjacent statements (stream_events' setup is
    # sync up to the first ``await`` inside ``send_initial``).
    snapshot = list(job.output)
    is_terminal = job.status in TERMINAL_JOB_STATUSES
    terminal_status = job.status.value if is_terminal else ""
    terminal_exit_code = job.exit_code
    terminal_error = job.error if is_terminal else None

    async def _send_initial(controls: StreamControls) -> None:
        for line in snapshot:
            await client.send_event(message_id, StreamEvent.OUTPUT, line)
        if is_terminal:
            await client.send_event(
                message_id,
                StreamEvent.RESULT,
                {
                    "status": terminal_status,
                    "exit_code": terminal_exit_code,
                    # ``error`` carries the human-readable
                    # failure reason :func:`_fail_locally` /
                    # ``_finalize_terminal`` stamped on the job
                    # (e.g. ``"remote build: peer-link session
                    # lost (transport_error: ...)"``). The
                    # frontend install dialog surfaces this in
                    # its red error banner; without the field
                    # the banner falls back to a generic
                    # "Install failed." that misattributes a
                    # receiver-restart to a broken build env.
                    # ``None`` for successful jobs and for
                    # jobs from before the field was set;
                    # frontend treats both equivalently.
                    "error": terminal_error,
                },
            )
            # No live drain — already-terminal job has nothing
            # more to deliver; end the stream so the helper
            # returns instead of parking on ``queue.get``.
            controls.end()

    def _handle_event(event: Event, controls: StreamControls) -> None:
        if event.event_type == EventType.JOB_OUTPUT:
            if event.data.get("job_id") == job_id:
                controls.push(StreamEvent.OUTPUT, event.data["line"])
        elif event.event_type in TERMINAL_JOB_EVENTS:
            ev_job = event.data.get("job")
            if ev_job and getattr(ev_job, "job_id", None) == job_id:
                status = getattr(ev_job, "status", "unknown")
                status_val = status.value if hasattr(status, "value") else str(status)
                controls.push_priority(
                    StreamEvent.RESULT,
                    {
                        "status": status_val,
                        "exit_code": getattr(ev_job, "exit_code", None),
                        "error": getattr(ev_job, "error", None),
                    },
                )
                controls.end()

    await stream_events(
        client=client,
        message_id=message_id,
        bus=controller._db.bus,
        event_types=(EventType.JOB_OUTPUT, *TERMINAL_JOB_EVENTS),
        handle_event=_handle_event,
        send_initial=_send_initial,
    )


async def follow_jobs(
    controller: FirmwareController,
    *,
    client: Any = None,
    message_id: str = "",
    snapshot: bool = True,
) -> None:
    """
    Stream every job's lifecycle events to one client connection.

    Designed for a "manage compile tasks" panel: subscribe once
    and the frontend sees every queued / started / progress /
    completed / failed / cancelled event for every job, plus
    live ``output`` lines tagged with their ``job_id``.

    When ``snapshot`` is True (default), the controller's full
    retained set of jobs — both active and the trimmed terminal
    history — is replayed first so the panel paints the complete
    picture immediately after a page refresh, with no extra round
    trip to ``firmware/get_jobs``. Each event keeps the same
    ``job`` payload shape as the bus, so the frontend can update
    its in-memory map by ``job_id`` without extra queries.

    Runs until the client disconnects (which surfaces here as a
    ``CancelledError`` from ``send_event``).

    Race-free against concurrent jobs the same way ``follow_job``
    is: ``stream_events`` attaches listeners *before* the
    snapshot replay is awaited, so a ``JOB_*`` event firing
    during the snapshot loop queues through the listener
    instead of being lost. The earlier shape sent the snapshot
    first and only attached listeners afterwards, so a job
    completing mid-replay silently disappeared from the stream.
    """
    if client is None:
        return

    # Serialize the snapshot to dicts synchronously *before*
    # ``stream_events`` attaches listeners. Capturing the
    # ``FirmwareJob`` objects and calling ``to_dict()`` later
    # (inside ``send_initial``) is racy: between listener
    # attach and each ``to_dict()`` the runner can append to a
    # running job's ``output`` or transition its status — that
    # mutation is folded into the snapshot dict AND delivered
    # again via the listener, so the client sees the same line
    # twice. Dict-freeze here makes the snapshot atomic against
    # the producer (no awaits between freeze and listener
    # attach) and de-duplicates the handoff.
    snapshot_payloads = (
        [job.to_dict() for job in sorted(controller._jobs.values(), key=attrgetter("created_at"))]
        if snapshot
        else []
    )

    async def _send_initial(_controls: StreamControls) -> None:
        for payload in snapshot_payloads:
            await client.send_event(message_id, StreamEvent.SNAPSHOT, payload)

    def _handle_event(event: Event, controls: StreamControls) -> None:
        if event.event_type == EventType.JOB_OUTPUT:
            # Forward the bus event name through verbatim — the
            # all-jobs follower's wire protocol matches the
            # ``EventType`` value byte-for-byte for these high-
            # rate events. Pass the StrEnum member directly;
            # ``StreamControls.push`` accepts any str.
            controls.push(EventType.JOB_OUTPUT, event.data)
        elif event.event_type == EventType.JOB_PROGRESS:
            controls.push(EventType.JOB_PROGRESS, event.data)
        else:
            # Lifecycle event (queued/started/completed/failed/
            # cancelled). Use ``push_priority`` so a backlog of
            # ``job_output`` lines can't drop a status
            # transition — a missed ``job_completed`` would
            # leave the all-jobs panel stuck on the old status
            # forever (no resync after the initial snapshot).
            # Output/progress are tolerable to lose; status
            # transitions are not.
            job = event.data.get("job")
            if job is None:
                return
            payload = job.to_dict() if hasattr(job, "to_dict") else job
            controls.push_priority(event.event_type.value, payload)

    await stream_events(
        client=client,
        message_id=message_id,
        bus=controller._db.bus,
        event_types=(
            EventType.JOB_QUEUED,
            EventType.JOB_STARTED,
            *TERMINAL_JOB_EVENTS,
            EventType.JOB_OUTPUT,
            EventType.JOB_PROGRESS,
        ),
        handle_event=_handle_event,
        send_initial=_send_initial,
    )
