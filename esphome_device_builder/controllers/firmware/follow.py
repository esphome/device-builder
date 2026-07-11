"""Firmware-job WS streaming endpoints: follow_job + follow_jobs."""

from __future__ import annotations

from operator import attrgetter
from typing import TYPE_CHECKING, Any

from ...helpers.api import registered_stream
from ...helpers.async_ import run_in_executor
from ...helpers.event_bus import StreamControls, stream_events
from ...models import (
    TERMINAL_JOB_EVENTS,
    EventType,
    FirmwareJob,
    StreamEvent,
)
from .persistence import job_dict_without_output, read_job_output

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
    """Follow a job: replay history, stream new output (``tail -f``-style).

    Already-terminal jobs get one history send + a final result
    event, then end. Live jobs keep streaming until completion.

    Snapshot-then-subscribe ordering matters: the listener is
    attached *before* the history replay so lines fired during
    replay queue through the listener and land strictly after
    history. The earlier iterate-then-subscribe shape dropped
    every line appended during replay, and the gap widened
    forever after the first in-flight ``_trim_job_output`` reassign.
    """
    job = controller.state.jobs.get(job_id)
    if not job:
        msg = f"Job not found: {job_id}"
        raise ValueError(msg)

    # Register before the first await so a ``devices/stop_stream`` for
    # this id (the frontend fires it when the log dialog closes or
    # switches jobs) actually cancels the follow instead of leaving a
    # live job's tail streaming until it completes or the WS drops.
    with registered_stream(client, message_id):
        await _stream_job(controller, job, job_id=job_id, client=client, message_id=message_id)


async def follow_jobs(
    controller: FirmwareController,
    *,
    client: Any = None,
    message_id: str = "",
    snapshot: bool = True,
) -> None:
    """Stream every job's lifecycle events + live output to one client.

    With ``snapshot=True`` (default), the full retained job set
    (active + trimmed terminal history) is replayed first so a
    refresh paints immediately with no follow-up
    ``firmware/get_jobs``. Each event payload is bus-shape
    (``job_id``-keyed) so the frontend updates its in-memory map
    without extra queries.

    Runs until the client disconnects (surfaces as
    ``CancelledError`` from ``send_event``). Same
    snapshot-then-subscribe ordering as :func:`follow_job` — a
    ``JOB_*`` event firing during snapshot replay queues through
    the listener rather than being lost.
    """
    if client is None:
        return

    # Freeze the snapshot to dicts synchronously *before*
    # ``stream_events`` attaches listeners. Deferring ``to_dict()``
    # into ``send_initial`` would let the runner mutate a running
    # job between freeze and serialise — that mutation lands in
    # both the snapshot AND the listener, so the client sees the
    # same line twice.
    # Snapshots omit ``output``: the panel reads only metadata, and
    # log text is fetched per-job via ``follow_job``. Dropping it also
    # keeps a running job's live buffer off the snapshot wire.
    snapshot_payloads = (
        [
            job_dict_without_output(job)
            for job in sorted(controller.state.jobs.values(), key=attrgetter("created_at"))
        ]
        if snapshot
        else []
    )

    async def _send_initial(_controls: StreamControls) -> None:
        for payload in snapshot_payloads:
            await client.send_event(message_id, StreamEvent.SNAPSHOT, payload)

    def _handle_event(event: Event, controls: StreamControls) -> None:
        if event.event_type == EventType.JOB_OUTPUT:
            controls.push(EventType.JOB_OUTPUT, event.data)
        elif event.event_type == EventType.JOB_PROGRESS:
            controls.push(EventType.JOB_PROGRESS, event.data)
        else:
            # Lifecycle event — use ``push_priority`` so a backlog
            # of high-rate output/progress can't drop a status
            # transition. A missed JOB_COMPLETED leaves the panel
            # stuck on the old status forever (no resync after
            # the initial snapshot).
            job = event.data.get("job")
            if job is None:
                return
            payload = job_dict_without_output(job) if hasattr(job, "to_dict") else job
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


async def _stream_job(
    controller: FirmwareController,
    job: FirmwareJob,
    *,
    job_id: str,
    client: Any,
    message_id: str,
) -> None:
    """Replay history then tail live output for one job until it ends or is cancelled."""
    # Capture snapshot before ``stream_events`` attaches listeners.
    is_terminal = job.is_terminal
    snapshot = await _initial_snapshot(job, job_id)
    terminal_result = _terminal_result_payload(job) if is_terminal else None

    async def _send_initial(controls: StreamControls) -> None:
        for line in snapshot:
            await client.send_event(message_id, StreamEvent.OUTPUT, line)
        if terminal_result is not None:
            await client.send_event(message_id, StreamEvent.RESULT, terminal_result)
            # End the stream so the helper returns instead of
            # parking on ``queue.get`` — already-terminal job has
            # nothing more to deliver.
            controls.end()

    def _handle_event(event: Event, controls: StreamControls) -> None:
        if event.event_type == EventType.JOB_OUTPUT:
            if event.data.get("job_id") == job_id:
                controls.push(StreamEvent.OUTPUT, event.data["line"])
        elif event.event_type in TERMINAL_JOB_EVENTS:
            ev_job = event.data.get("job")
            if ev_job and getattr(ev_job, "job_id", None) == job_id:
                controls.push_priority(StreamEvent.RESULT, _terminal_result_payload(ev_job))
                controls.end()

    await stream_events(
        client=client,
        message_id=message_id,
        bus=controller._db.bus,
        event_types=(EventType.JOB_OUTPUT, *TERMINAL_JOB_EVENTS),
        handle_event=_handle_event,
        send_initial=_send_initial,
    )


def _terminal_result_payload(job: FirmwareJob) -> dict[str, Any]:
    """
    Build a terminal job's RESULT frame — snapshot and live paths share one shape.

    ``queued_update_armed`` reports whether the job armed a queued
    update, not the raw deferred flag.
    """
    return {
        "status": job.status.value,
        "exit_code": job.exit_code,
        "error": job.error,
        "queued_update_armed": job.is_queued_update_armed,
    }


async def _initial_snapshot(job: FirmwareJob, job_id: str) -> list[str]:
    """Output lines to replay before tailing live: RAM while present, else the sidecar.

    A live job's RAM buffer is frozen synchronously so the listener
    ``follow_job`` attaches next can't slip lines between freeze and
    subscribe. A terminal job's output is flushed to its sidecar and
    dropped from RAM by the post-completion persist, but the terminal
    event fires *before* that flush — so prefer RAM while it's still
    populated and fall back to the sidecar once cleared. The persist
    writes the sidecar then clears RAM in one executor pass, so RAM
    is non-empty xor the sidecar exists, never neither: no window
    where a just-finished job reads back an empty log.

    ``job.output`` is captured into a local first: the concurrent
    flush *rebinds* the attribute to a fresh ``[]``, so reading it
    twice (truthiness then ``list()``) could see the populated list,
    miss the sidecar branch, then capture the emptied one. The local
    keeps the pre-flush list reference regardless of when the rebind
    lands.
    """
    output = job.output
    if output:
        return list(output)
    if job.is_terminal:
        return await run_in_executor(read_job_output, job_id)
    return []
