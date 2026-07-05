"""
Receiver-side fan-out from firmware ``JOB_*`` events to peer-link frames.

Pairs with the receiver-side accept path
(:mod:`controllers.remote_build.submit_job`): once a remote-peer
:class:`FirmwareJob` is queued (carrying ``remote_peer`` +
``remote_job_id``), the firmware controller's existing
:attr:`EventType.JOB_*` events drive lifecycle and output
streams. This module subscribes to those events, filters to
jobs whose ``remote_peer`` matches an active peer-link
session, and forwards each one as a typed
:class:`JobStateChangedFrameData` / :class:`JobOutputFrameData`
back to the submitting offloader over the same session.

Wiring:

* Lifecycle events ``JOB_QUEUED`` / ``JOB_STARTED`` /
  ``JOB_COMPLETED`` / ``JOB_FAILED`` / ``JOB_CANCELLED`` map 1:1
  to ``job_state_changed`` frames with ``status`` = ``queued`` /
  ``running`` / ``completed`` / ``failed`` / ``cancelled``. The
  ``queued`` fan-out is what drives the offloader's "waiting in
  line" screen when the receiver is busy with another
  offloader's job.
* ``JOB_OUTPUT`` maps to ``job_output{stream, line}``. High-
  rate during an active build (one per line of compiler /
  linker output); the channel's per-frame Noise AEAD overhead
  is the dominant cost. A future optimisation can batch
  consecutive lines into one frame, but the wire shape is
  one-line-per-frame today (matches 5c-1's
  :class:`JobOutputFrameData` contract).

The fan-out is **best-effort**: a session that's gone away
(unregistered between the JOB_* fire and the lookup, or the
``send_app_frame`` itself failing) is logged at debug and
skipped — the job runs to completion either way; the
offloader's missing-output is on the next session bring-up's
problem to surface (or 5d's cancel path to mop up).
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from typing import TYPE_CHECKING, Literal

from ...helpers.event_bus import Event
from ...models import (
    TERMINAL_JOB_EVENTS,
    EventType,
    JobFailureReason,
    JobLifecycleData,
    JobOutputData,
    JobOutputFrameData,
    JobStateChangedFrameData,
)

if TYPE_CHECKING:
    from .peer_link import PeerLinkSession
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)


# Map ``EventType`` lifecycle members to the wire-side
# ``JobStateChangedFrameData.status`` value. The receiver
# fires the bus event from the firmware queue's existing
# transitions; this map turns each into the typed frame's
# enum string.
_JobStatusLiteral = Literal["queued", "running", "completed", "failed", "cancelled"]

_LIFECYCLE_EVENT_TO_STATUS: dict[EventType, _JobStatusLiteral] = {
    EventType.JOB_QUEUED: "queued",
    EventType.JOB_STARTED: "running",
    EventType.JOB_COMPLETED: "completed",
    EventType.JOB_FAILED: "failed",
    EventType.JOB_CANCELLED: "cancelled",
}


class JobFanout:
    """Subscribes to firmware ``JOB_*`` events and forwards remote-peer jobs.

    One instance per :class:`ReceiverController` (started
    in :meth:`ReceiverController.start`). Holds the
    unsubscribe handles for the firmware bus listeners so
    :meth:`stop` can detach them on controller shutdown.

    The session lookup keys on ``FirmwareJob.remote_peer`` (the
    offloader's ``dashboard_id``) against
    ``ReceiverController.state.peer_link_sessions``. A job whose
    ``remote_peer`` is empty is local-only and skipped before
    any session lookup.
    """

    def __init__(self, controller: ReceiverController) -> None:
        self._controller = controller
        # Lifecycle bag of bus-listener unsubs. Each
        # ``EventBus.add_listener`` return is a sync callable
        # that drops the listener; ``ExitStack.callback`` is
        # the stdlib pattern for accumulating those across
        # ``start`` and walking them on ``stop``.
        self._listeners = ExitStack()
        # Receiver-side ``FirmwareJob.job_id`` →
        # ``(remote_peer, remote_job_id)`` for every in-flight
        # remote-peer job. Populated on JOB_QUEUED (the first
        # event for any job) so :meth:`_on_output`'s hot-path
        # lookup is a sync dict access against state we own,
        # not a linear scan through ``firmware.find_remote_peer_job``
        # on every output line. Dropped on terminal events so a
        # never-emitted JOB_OUTPUT for
        # a long-finished job doesn't keep the entry around
        # forever (the firmware controller retains job rows
        # for post-mortem inspection).
        self._remote_jobs: dict[str, tuple[str, str]] = {}

    def start(self) -> None:
        """Attach listeners on the firmware controller's bus.

        Not idempotent — calling twice would double-subscribe
        each listener and double-fire every fan-out frame. Single
        caller is :meth:`ReceiverController.start`. Listener
        lifetime is bounded by the controller's start / stop
        cycle; :meth:`stop` detaches every captured handle.
        """
        bus = self._controller._db.bus
        if bus is None:
            return
        # JOB_QUEUED has a dedicated handler so the cache
        # populate runs before the fan-out (listener order on
        # the bus is not defined).
        self._listeners.callback(bus.add_listener(EventType.JOB_QUEUED, self._on_queued))
        for event_type in _LIFECYCLE_EVENT_TO_STATUS:
            if event_type is EventType.JOB_QUEUED:
                continue
            self._listeners.callback(bus.add_listener(event_type, self._on_lifecycle))
        self._listeners.callback(bus.add_listener(EventType.JOB_OUTPUT, self._on_output))

    def stop(self) -> None:
        """Drop every listener registered by :meth:`start` and clear the cache."""
        self._listeners.close()
        self._remote_jobs.clear()

    def resolve_firmware_job_id(self, remote_peer: str, remote_job_id: str) -> str | None:
        """Return the receiver-local ``FirmwareJob.job_id`` for an offloader-side correlation.

        Reverse lookup over the forward
        ``firmware_job_id → (remote_peer, remote_job_id)``
        cache. Used by the 5d ``cancel_job`` dispatch path —
        given the ``session.dashboard_id`` and the
        offloader-supplied ``job_id`` from the wire frame,
        find the matching receiver-local id so
        :meth:`FirmwareController.cancel` can route the
        cancellation through the existing primitive.

        Linear scan rather than a maintained reverse index:
        the cache only ever holds in-flight remote-driven
        jobs (terminal entries drop on the matching event),
        and the firmware queue's serial execution caps the
        live set at "one running + queue_depth" per receiver.
        A maintained reverse index would add a second mutation
        site on every queued / terminal transition for a
        constant-time saving that doesn't show up on the
        practical cardinality.

        Returns ``None`` when no match exists — typically a
        race between the offloader's cancel send and a
        receiver-side terminal transition that already
        evicted the entry.
        """
        for firmware_job_id, (peer, rjid) in self._remote_jobs.items():
            if peer == remote_peer and rjid == remote_job_id:
                return firmware_job_id
        return None

    def _on_queued(self, event: Event[JobLifecycleData]) -> None:
        """Cache the remote-peer correlation for *job* and fan out ``queued``.

        The ``queued`` frame drives the offloader's "waiting in
        line" screen when the receiver is busy with another
        offloader's job. Local-only jobs and jobs missing
        ``remote_job_id`` are skipped.
        """
        job = event.data["job"]
        if not job.remote_peer:
            return
        if not job.remote_job_id:
            _LOGGER.debug(
                "JOB_QUEUED for remote peer %s (job %s) missing remote_job_id; "
                "fan-out will skip this job's lifecycle and output events",
                job.remote_peer,
                job.job_id,
            )
            return
        self._remote_jobs[job.job_id] = (job.remote_peer, job.remote_job_id)
        self._dispatch_state_changed(
            remote_peer=job.remote_peer,
            remote_job_id=job.remote_job_id,
            status="queued",
            error_message="",
            failure_reason=JobFailureReason.NONE,
            log_label=event.event_type.value,
            log_job_id=job.job_id,
        )

    def _on_lifecycle(self, event: Event[JobLifecycleData]) -> None:
        """Forward a lifecycle transition to the submitting session, best-effort.

        Bus listener — runs synchronously inside
        :meth:`EventBus.fire`. The actual frame send is async
        and happens via
        :meth:`DeviceBuilder.create_background_task` so the
        listener returns promptly and doesn't block the firing
        coroutine on a slow socket. Drops the
        :attr:`_remote_jobs` cache entry on terminal events so a
        retained ``FirmwareJob`` row (kept for post-mortem
        inspection) doesn't keep the per-job correlation tuple
        live forever.
        """
        job = event.data["job"]
        # Snapshot the cache entry BEFORE popping on terminal
        # events so the terminal frame itself still fans out —
        # the offloader needs the ``completed`` / ``failed`` /
        # ``cancelled`` frame, then the cache entry can go.
        entry = self._remote_jobs.get(job.job_id)
        if event.event_type in TERMINAL_JOB_EVENTS:
            self._remote_jobs.pop(job.job_id, None)
        if entry is None:
            return
        remote_peer, remote_job_id = entry
        status = _LIFECYCLE_EVENT_TO_STATUS[event.event_type]
        # ``error_message`` is the empty string on non-terminal
        # paths; populate from ``FirmwareJob.error`` on
        # ``failed`` / ``cancelled`` so the offloader has a
        # one-liner to surface without parsing the full output.
        error_message = job.error if status in {"failed", "cancelled"} and job.error else ""
        # Machine category (e.g. ``"provision"``) lets the offloader treat a
        # receiver-side provision failure as retryable and rebuild locally.
        failure_reason = job.failure_reason if status == "failed" else JobFailureReason.NONE
        self._dispatch_state_changed(
            remote_peer=remote_peer,
            remote_job_id=remote_job_id,
            status=status,
            error_message=error_message,
            failure_reason=failure_reason,
            log_label=event.event_type.value,
            log_job_id=job.job_id,
        )

    def _dispatch_state_changed(
        self,
        *,
        remote_peer: str,
        remote_job_id: str,
        status: _JobStatusLiteral,
        error_message: str,
        failure_reason: JobFailureReason,
        log_label: str,
        log_job_id: str,
    ) -> None:
        """Send a ``job_state_changed`` frame to *remote_peer*'s session, best-effort."""
        session = self._controller.state.peer_link_sessions.get(remote_peer)
        if session is None:
            _LOGGER.debug(
                "%s for remote peer %s (job %s): no active session; dropping fan-out",
                log_label,
                remote_peer,
                log_job_id,
            )
            return
        frame = JobStateChangedFrameData(
            type="job_state_changed",
            job_id=remote_job_id,
            status=status,
            error_message=error_message,
        )
        if failure_reason is not JobFailureReason.NONE:
            frame["failure_reason"] = failure_reason.value
        self._dispatch(session, frame)

    def _on_output(self, event: Event[JobOutputData]) -> None:
        """Forward one output line to the submitting session, best-effort.

        Hot path — fires at 100+ events/sec on a cold compile.
        The lookup is a single sync dict access against
        :attr:`_remote_jobs` populated on JOB_QUEUED; no
        cross-controller reach.
        """
        # Cache miss → local job, or remote-peer job that
        # finished before the listeners attached. Session miss →
        # offloader unregistered mid-build. Both are silent
        # drops; the lifecycle path logs for the latter (output
        # fires per-line and would flood the log).
        entry = self._remote_jobs.get(event.data["job_id"])
        if entry is None:
            return
        remote_peer, remote_job_id = entry
        session = self._controller.state.peer_link_sessions.get(remote_peer)
        if session is None:
            return
        # The firmware controller's ``JOB_OUTPUT`` doesn't
        # carry a ``stream`` discriminator today — every line
        # arrives as one merged stdout-shaped feed. Ship as
        # ``stdout``; if the receiver later starts producing
        # stderr-tagged lines, the wire shape is ready for
        # them without another schema bump.
        self._dispatch(
            session,
            JobOutputFrameData(
                type="job_output",
                job_id=remote_job_id,
                stream="stdout",
                line=event.data["line"],
            ),
        )

    def _dispatch(
        self,
        session: PeerLinkSession,
        payload: JobStateChangedFrameData | JobOutputFrameData,
    ) -> None:
        """Schedule a wire-frame send as a background task on the controller's loop."""
        self._controller._db.create_background_task(self._send_app_frame(session, dict(payload)))

    @staticmethod
    async def _send_app_frame(session: PeerLinkSession, payload: dict[str, object]) -> None:
        """Send *payload* over *session*, swallowing per-frame failures.

        ``send_app_frame`` already returns ``False`` on the
        common transport / encrypt / serialise failures and
        logs at the channel layer; the bare ``except`` here is
        the catch-all for an unexpected raise (e.g. a future
        code path that raises before the inner gate). Logged
        at debug — the job runs to completion regardless of
        whether each fan-out frame lands.
        """
        try:
            await session.send_app_frame(payload)
        except Exception:
            _LOGGER.debug(
                "fan-out send_app_frame raised for session %s; dropping",
                session.dashboard_id,
                exc_info=True,
            )
