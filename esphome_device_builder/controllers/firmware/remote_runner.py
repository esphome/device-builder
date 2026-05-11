"""
Source-routed runner branch for ``JobSource.REMOTE`` firmware jobs.

Lives in its own module so ``controller.py`` stays focused on the
local subprocess pipeline + WS surface. The branch is invoked
from :meth:`FirmwareController._execute_job` when the job's
``source`` field is ``REMOTE``, after ``JOB_STARTED`` has fired
and before chip-verify; it returns once the receiver has emitted
a terminal ``OFFLOADER_JOB_STATE_CHANGED`` (or a translatable
failure path has been finalised locally).

The receiver doesn't know about the offloader-side
:class:`FirmwareJob` — it tracks its own row keyed by its own
``job_id``, and echoes the offloader's id back on every
fan-out frame so the offloader can correlate. We use
``job.job_id`` (the offloader-side id) as the match key on
inbound :class:`OffloaderJobOutputData` /
:class:`OffloaderJobStateChangedData` events.

Listener attach happens **before** ``submit_job`` is sent so an
immediate ``running`` / ``output`` frame from the receiver
can't outrace our subscription. The listener bucket is
process-wide; a stray frame from a different in-flight remote
job lands in a different match-id / pin combination and is
filtered out at the callback boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.config_bundle import BundleBuildError, build_yaml_bundle
from ...models import (
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobStatus,
    JobType,
    OffloaderJobOutputData,
    OffloaderJobStateChangedData,
    OffloaderPeerLinkClosedData,
)
from ..remote_build.peer_link_client import (
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
)
from .helpers import _ingest_output_line, _mark_job_terminal

if TYPE_CHECKING:
    from ...helpers.event_bus import Event
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)

# Terminal receiver-side statuses on
# :class:`OffloaderJobStateChangedData`. Mirror of
# :data:`TERMINAL_JOB_STATUSES` but on the wire literal rather
# than the local enum — receiver-side fan-out emits the lower-
# case string per :class:`JobStateChangedFrameData`'s
# ``Literal`` union.
_TERMINAL_WIRE_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


async def run_remote_compile_job(
    controller: FirmwareController,
    job: FirmwareJob,
) -> None:
    """Run a REMOTE-source compile job and finalise *job* on the offloader bus.

    Caller (``FirmwareController._execute_job``) has already
    set ``status = RUNNING`` and fired ``JOB_STARTED``; this
    function is responsible for the entire run-and-finalise
    middle and leaves the outer ``finally`` block to clear
    ``_current_job`` / persist.
    """
    if job.job_type is not JobType.COMPILE:
        _fail_locally(
            controller,
            job,
            error=f"remote source supports only COMPILE (got {job.job_type.value})",
        )
        return

    if not job.source_pin_sha256:
        _fail_locally(
            controller,
            job,
            error="remote source missing source_pin_sha256",
        )
        return

    bus = controller.bus
    pin = job.source_pin_sha256
    target_job_id = job.job_id
    loop = asyncio.get_running_loop()
    terminal: asyncio.Future[OffloaderJobStateChangedData] = loop.create_future()
    # Set when the peer-link session backing this job's
    # receiver closes. Distinct from ``terminal`` because the
    # receiver hasn't given us a structured terminal status —
    # we have to synthesise the lost-session failure on the
    # offloader side. ``_await_terminal`` waits on whichever
    # fires first.
    session_lost: asyncio.Future[OffloaderPeerLinkClosedData] = loop.create_future()
    # Set by ``FirmwareController.cancel`` when the user clicks
    # Stop. Registering it on the controller lets the cancel
    # handler signal the parked runner instantly instead of
    # waiting for a polling cadence — the runner parks on
    # ``asyncio.wait({terminal, session_lost, cancel_event_task})``
    # and wakes on whichever fires first.
    cancel_event = asyncio.Event()
    controller._cancel_events[job.job_id] = cancel_event
    # A cancel that landed during ``_execute_job``'s pre-runner
    # phase (``_current_job = job`` is set before the persist
    # await, so the cancel handler accepts the request and
    # writes to ``_cancel_requested`` — but the runner hasn't
    # yet installed its event, so the handler's
    # ``_cancel_events.get(job_id)`` returns ``None`` and the
    # ``set()`` is skipped). Replay the late wake here so the
    # runner doesn't park on an event that will never fire.
    if job.job_id in controller._cancel_requested:
        cancel_event.set()

    def _is_ours(data: OffloaderJobOutputData | OffloaderJobStateChangedData) -> bool:
        """Return True if *data* belongs to this runner's (pin, job_id) pair."""
        return data["pin_sha256"] == pin and data["job_id"] == target_job_id

    def _on_output(event: Event[OffloaderJobOutputData]) -> None:
        if not _is_ours(event.data):
            return
        _ingest_output_line(job, bus, event.data["line"])

    def _on_state(event: Event[OffloaderJobStateChangedData]) -> None:
        data = event.data
        if not _is_ours(data):
            return
        if data["status"] in _TERMINAL_WIRE_STATUSES and not terminal.done():
            terminal.set_result(data)

    def _on_session_closed(event: Event[OffloaderPeerLinkClosedData]) -> None:
        # Pin-only filter — session-close events don't carry a
        # job_id (the close brings down every in-flight job on
        # the link). ``_is_ours`` keys on job_id too, so this
        # one stays inline.
        data = event.data
        if data["pin_sha256"] != pin or session_lost.done():
            return
        session_lost.set_result(data)

    try:
        with (
            bus.listening([EventType.OFFLOADER_JOB_OUTPUT], _on_output),
            bus.listening([EventType.OFFLOADER_JOB_STATE_CHANGED], _on_state),
            bus.listening([EventType.OFFLOADER_PEER_LINK_CLOSED], _on_session_closed),
        ):
            try:
                await _dispatch_and_drive(
                    controller=controller,
                    job=job,
                    terminal=terminal,
                    session_lost=session_lost,
                    cancel_event=cancel_event,
                )
            except asyncio.CancelledError:
                # Runner-task shutdown (controller stop). Mirror the
                # local subprocess path's contract: finalise the job
                # as CANCELLED so subscribers see a terminal event,
                # then re-raise to let the outer runner unwind.
                controller._finalize_cancelled(job)
                _LOGGER.info("Remote job %s cancelled (runner shutdown)", job.job_id)
                raise
    finally:
        # Clean up the registration so a future job reusing the
        # id (theoretical — uuid4 collision aside) doesn't
        # signal a stale event.
        controller._cancel_events.pop(job.job_id, None)


async def _dispatch_and_drive(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    terminal: asyncio.Future[OffloaderJobStateChangedData],
    session_lost: asyncio.Future[OffloaderPeerLinkClosedData],
    cancel_event: asyncio.Event,
) -> None:
    """Build the bundle, submit, then wait for the receiver's terminal frame.

    Split out from :func:`run_remote_compile_job` so the
    listener attach / detach lives in one ``with`` block at the
    outer call site — every early-return failure path here
    still releases the bus subscriptions.
    """
    loop = asyncio.get_running_loop()
    yaml_path = await loop.run_in_executor(
        None, controller._db.settings.rel_path, job.configuration
    )

    try:
        bundle_bytes = await build_yaml_bundle(yaml_path)
    except FileNotFoundError:
        _fail_locally(
            controller,
            job,
            error=f"remote build: configuration not found: {job.configuration}",
        )
        return
    except BundleBuildError as exc:
        _fail_locally(
            controller,
            job,
            error=f"remote build: bundle failed: {exc.output or exc}",
        )
        return

    remote_build = controller._db.remote_build
    if remote_build is None:
        _fail_locally(
            controller,
            job,
            error="remote build: controller not initialised",
        )
        return
    try:
        client = remote_build._lookup_open_peer_link_client(
            job.source_pin_sha256, label="firmware_remote"
        )
    except CommandError as exc:
        _fail_locally(
            controller,
            job,
            error=f"remote build: receiver not reachable: {exc.message}",
        )
        return

    try:
        ack = await client.submit_job(
            job_id=job.job_id,
            configuration_filename=job.configuration,
            target="compile",
            bundle_bytes=bundle_bytes,
        )
    except (PeerLinkNoSessionError, SubmitJobTimeoutError, SubmitJobSessionLostError) as exc:
        _fail_locally(
            controller,
            job,
            error=f"remote build: dispatch failed: {exc}",
        )
        return

    if not ack["accepted"]:
        reason = ack.get("reason", "no reason given")
        _fail_locally(
            controller,
            job,
            error=f"remote build: receiver rejected job: {reason}",
        )
        return

    await _await_terminal(
        controller=controller,
        job=job,
        terminal=terminal,
        session_lost=session_lost,
        cancel_event=cancel_event,
    )


async def _await_terminal(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    terminal: asyncio.Future[OffloaderJobStateChangedData],
    session_lost: asyncio.Future[OffloaderPeerLinkClosedData],
    cancel_event: asyncio.Event,
) -> None:
    """
    Wait for the receiver's terminal state, translating local cancel to the wire.

    Parks on ``asyncio.wait({terminal, session_lost,
    cancel_event.wait()}, return_when=FIRST_COMPLETED)`` —
    event-driven, no polling cadence. The cancel handler
    (``FirmwareController.cancel``) signals *cancel_event*
    when the user clicks Stop, so the runner wakes
    instantly instead of waiting up to half a second for a
    poll iteration. ``session_lost`` is the synthetic
    failure path the offloader uses when the peer-link
    session closes before the receiver can emit a terminal
    frame; without it the wait would deadlock on a dead
    receiver.

    The runner cooperates with whichever of *terminal* /
    *session_lost* / *cancel_event* fires first; the
    branches below decide the final job status.
    """
    cancel_sent = False
    bus = controller.bus
    # ``asyncio.wait`` is invariant on its element type; the
    # heterogeneous awaitables (two TypedDict futures + one
    # Event-wait coroutine wrapped as a Task) are widened to
    # ``Any`` at the call so mypy doesn't reject the mix.
    cancel_wait = asyncio.get_running_loop().create_task(cancel_event.wait())
    waiters: list[asyncio.Future[Any]] = [terminal, session_lost, cancel_wait]
    try:
        while not terminal.done():
            if session_lost.done():
                closed = session_lost.result()
                reason = closed["reason"]
                detail = closed["error_detail"]
                text = f"{reason}: {detail}" if detail else reason
                _fail_locally(
                    controller,
                    job,
                    error=f"remote build: peer-link session lost ({text})",
                )
                return
            if cancel_event.is_set() and not cancel_sent:
                cancel_sent = True
                if not await _send_cancel_or_finalise(controller, job):
                    return
                # After dispatching the wire cancel we only care
                # about ``terminal`` / ``session_lost`` — the
                # cancel-event signal has already done its job.
                waiters = [terminal, session_lost]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not cancel_wait.done():
            cancel_wait.cancel()

    data = terminal.result()
    if job.job_id in controller._cancel_requested:
        # User cancel beat the receiver's terminal frame to the
        # loop (receiver completed / failed while our cancel was
        # in flight). Mirror the local subprocess path: user
        # intent wins, finalise as CANCELLED regardless of the
        # status we received.
        controller._finalize_cancelled(job)
        return
    status = data["status"]
    if status == "completed":
        _mark_job_terminal(job, JobStatus.COMPLETED)
        payload: JobLifecycleData = {"job": job}
        bus.fire(EventType.JOB_COMPLETED, payload)
    elif status == "cancelled":
        controller._finalize_cancelled(job)
    else:
        # ``failed`` — the only other element in
        # :data:`_TERMINAL_WIRE_STATUSES`. Receiver-supplied
        # error text rides into ``job.error``; an empty
        # ``error_message`` (older receiver, internal bug)
        # falls back to a generic string so subscribers always
        # see a non-empty reason.
        job.error = data["error_message"] or "remote build failed"
        _mark_job_terminal(job, JobStatus.FAILED)
        failed_payload: JobLifecycleData = {"job": job}
        bus.fire(EventType.JOB_FAILED, failed_payload)


async def _send_cancel_or_finalise(
    controller: FirmwareController,
    job: FirmwareJob,
) -> bool:
    """
    Translate a pending local cancel into a wire ``cancel_job``.

    Returns ``True`` if the cancel frame is on the wire and the
    caller should keep waiting for the receiver's cancelled
    terminal frame; ``False`` if the local side already
    finalised the job (because there's no live session to
    cancel against, or the controller was torn down). Splits
    out from :func:`_await_terminal`'s loop so the lookup-vs-
    send error paths each get their own ``except`` clause
    without nesting.
    """
    remote_build = controller._db.remote_build
    if remote_build is None:
        # Receiver controller torn down mid-run — finalise as
        # cancelled rather than spinning forever on a future
        # that nothing will set.
        controller._finalize_cancelled(job)
        return False
    try:
        client = remote_build._lookup_open_peer_link_client(
            job.source_pin_sha256, label="firmware_remote_cancel"
        )
        await client.cancel_job(job_id=job.job_id)
    except (CommandError, PeerLinkNoSessionError) as exc:
        # ``CommandError`` from the lookup means the receiver
        # is unpaired / mid-reconnect; ``PeerLinkNoSessionError``
        # from the send means the session dropped between the
        # lookup and the wire write. Both translate the user's
        # Stop click into a local CANCELLED finalise — without
        # this fallback the cancel sits forever waiting for a
        # confirmation frame that will never arrive.
        detail = exc.message if isinstance(exc, CommandError) else str(exc)
        _LOGGER.info(
            "remote cancel for job %s: peer-link unavailable (%s); finalising locally",
            job.job_id,
            detail,
        )
        controller._finalize_cancelled(job)
        return False
    return True


def _fail_locally(
    controller: FirmwareController,
    job: FirmwareJob,
    *,
    error: str,
) -> None:
    """Mark *job* FAILED with *error* and fire ``JOB_FAILED`` on the local bus.

    Centralises the "remote path can't proceed, finalise
    terminally" sequence so every early-exit failure branch
    above stays one line at the call site. The text rides
    into ``job.error`` so a frontend that already renders
    ``error`` for local failures shows the remote failure
    with no special-case code.

    Cancel intent wins: if the user already flipped
    ``_cancel_requested`` for this job (Stop click landed
    during bundle build / lookup / dispatch / session-lost
    detection — anywhere before the receiver could emit a
    terminal frame), finalise as CANCELLED instead. Mirrors
    the local subprocess path's contract — a Stop that
    happened to race a failure should not show up as a red
    error badge.
    """
    if job.job_id in controller._cancel_requested:
        controller._finalize_cancelled(job)
        _LOGGER.info("Remote job %s cancelled (failure path: %s)", job.job_id, error)
        return
    job.error = error
    _mark_job_terminal(job, JobStatus.FAILED)
    payload: JobLifecycleData = {"job": job}
    controller.bus.fire(EventType.JOB_FAILED, payload)
    _LOGGER.warning("Remote job %s failed: %s", job.job_id, error)
