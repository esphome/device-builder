"""
Receiver-side fan-out from firmware ``JOB_*`` events to peer-link frames.

Phase 5c-2b of issue #106. Pairs with the 5c-2a accept path
(:mod:`controllers.remote_build_submit_job`): once a remote-peer
:class:`FirmwareJob` is queued (carrying ``remote_peer`` +
``remote_job_id``), the firmware controller's existing
:attr:`EventType.JOB_*` events drive lifecycle and output
streams. This module subscribes to those events, filters to
jobs whose ``remote_peer`` matches an active peer-link
session, and forwards each one as a typed
:class:`JobStateChangedFrameData` / :class:`JobOutputFrameData`
back to the submitting offloader over the same session.

Wiring:

* Lifecycle events ``JOB_STARTED`` / ``JOB_COMPLETED`` /
  ``JOB_FAILED`` / ``JOB_CANCELLED`` map 1:1 to
  ``job_state_changed`` frames with ``status`` = ``running`` /
  ``completed`` / ``failed`` / ``cancelled``. ``JOB_QUEUED``
  doesn't fan out — the ``submit_job_ack`` already tells the
  offloader the job is queued, so a redundant
  ``job_state_changed{queued}`` frame would race the ack and
  add wire noise without adding signal.
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
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from ..helpers.event_bus import Event
from ..models import (
    EventType,
    JobLifecycleData,
    JobOutputData,
    JobOutputFrameData,
    JobStateChangedFrameData,
)

if TYPE_CHECKING:
    from .remote_build import RemoteBuildController
    from .remote_build_peer_link import PeerLinkSession

_LOGGER = logging.getLogger(__name__)


# Map ``EventType`` lifecycle members to the wire-side
# ``JobStateChangedFrameData.status`` value. The receiver
# fires the bus event from the firmware queue's existing
# transitions; this map turns each into the typed frame's
# enum string. ``JOB_QUEUED`` is intentionally absent — the
# ``submit_job_ack`` already tells the offloader the job is
# queued (see module docstring).
_JobStatusLiteral = Literal["queued", "running", "completed", "failed", "cancelled"]

_LIFECYCLE_EVENT_TO_STATUS: dict[EventType, _JobStatusLiteral] = {
    EventType.JOB_STARTED: "running",
    EventType.JOB_COMPLETED: "completed",
    EventType.JOB_FAILED: "failed",
    EventType.JOB_CANCELLED: "cancelled",
}


class JobFanout:
    """Subscribes to firmware ``JOB_*`` events and forwards remote-peer jobs.

    One instance per :class:`RemoteBuildController` (started
    in :meth:`RemoteBuildController.start`). Holds the
    unsubscribe handles for the firmware bus listeners so
    :meth:`stop` can detach them on controller shutdown.

    The session lookup keys on ``FirmwareJob.remote_peer`` (the
    offloader's ``dashboard_id``) against
    ``RemoteBuildController._peer_link_sessions``. A job whose
    ``remote_peer`` is empty is local-only and skipped before
    any session lookup.
    """

    def __init__(self, controller: RemoteBuildController) -> None:
        self._controller = controller
        # Captured ``EventBus.add_listener`` returns; each is a
        # callable that drops the listener when invoked.
        # Populated by :meth:`start`, walked by :meth:`stop`.
        self._unsubscribers: list[Callable[[], None]] = []

    def start(self) -> None:
        """Attach listeners on the firmware controller's bus.

        Not idempotent — calling twice would double-subscribe
        each listener and double-fire every fan-out frame. Single
        caller is :meth:`RemoteBuildController.start`. Listener
        lifetime is bounded by the controller's start / stop
        cycle; :meth:`stop` detaches every captured handle.
        """
        bus = self._controller._db.bus
        if bus is None:
            return
        for event_type in _LIFECYCLE_EVENT_TO_STATUS:
            self._unsubscribers.append(bus.add_listener(event_type, self._on_lifecycle))
        self._unsubscribers.append(bus.add_listener(EventType.JOB_OUTPUT, self._on_output))

    def stop(self) -> None:
        """Drop every listener registered by :meth:`start`."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()

    def _on_lifecycle(self, event: Event[JobLifecycleData]) -> None:
        """Forward a lifecycle transition to the submitting session, best-effort.

        Bus listener — runs synchronously inside
        :meth:`EventBus.fire`. The actual frame send is async
        and happens via
        :meth:`DeviceBuilder.create_background_task` so the
        listener returns promptly and doesn't block the firing
        coroutine on a slow socket.
        """
        job = event.data["job"]
        if not job.remote_peer or not job.remote_job_id:
            return
        status = _LIFECYCLE_EVENT_TO_STATUS[event.event_type]
        session = self._controller._peer_link_sessions.get(job.remote_peer)
        if session is None:
            _LOGGER.debug(
                "JOB_%s for remote peer %s: no active session; dropping fan-out",
                job.status,
                job.remote_peer,
            )
            return
        # ``error_message`` is the empty string on non-terminal
        # paths; populate from ``FirmwareJob.error`` on
        # ``failed`` / ``cancelled`` so the offloader has a
        # one-liner to surface without parsing the full output.
        error_message = job.error if status in {"failed", "cancelled"} and job.error else ""
        payload: JobStateChangedFrameData = {
            "type": "job_state_changed",
            "job_id": job.remote_job_id,
            "status": status,
            "error_message": error_message,
        }
        self._controller._db.create_background_task(self._send_app_frame(session, dict(payload)))

    def _on_output(self, event: Event[JobOutputData]) -> None:
        """Forward one output line to the submitting session, best-effort.

        Looks up the owning :class:`FirmwareJob` from
        ``event.data["job_id"]`` so the listener doesn't have
        to receive the full job object on every line. Output
        events fire at high rate during active builds (100+
        per second on a cold compile), so the lookup is the
        common path.
        """
        job_id = event.data["job_id"]
        firmware = self._controller._db.firmware
        if firmware is None:
            return
        # ``firmware._jobs`` is the canonical in-memory job map;
        # the public ``firmware.get_job`` is async (``@api_command``-
        # shaped) and overkill for a sync dict lookup that fires
        # 100+ times/sec on a hot compile. Reaching through the
        # underscore here is deliberate, matches the cross-
        # controller access pattern elsewhere in this package
        # (the peer-link receive loop reaches into
        # ``session._channel``).
        job = firmware._jobs.get(job_id)
        if job is None or not job.remote_peer or not job.remote_job_id:
            return
        session = self._controller._peer_link_sessions.get(job.remote_peer)
        if session is None:
            return
        # The firmware controller's ``JOB_OUTPUT`` doesn't
        # carry a ``stream`` discriminator today — every line
        # arrives as one merged stdout-shaped feed. Ship as
        # ``stdout``; if the receiver later starts producing
        # stderr-tagged lines, the wire shape is ready for
        # them without another schema bump.
        payload: JobOutputFrameData = {
            "type": "job_output",
            "job_id": job.remote_job_id,
            "stream": "stdout",
            "line": event.data["line"],
        }
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
