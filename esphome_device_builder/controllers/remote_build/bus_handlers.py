"""
Offloader-side bus event handler bodies.

Five callbacks the controller registers in :meth:`start`:
- ``_on_offloader_pair_pin_mismatch`` — caches the alert for
  late-subscriber snapshot.
- ``_on_offloader_peer_link_opened`` — tracks open sessions
  and refreshes the receiver's ``esphome_version``.
- ``_on_offloader_peer_link_closed`` — clears the open-session
  tracking.
- ``_on_offloader_queue_status_changed`` — updates the
  per-peer queue-status cache.
- ``_on_offloader_job_state_changed`` — maintains the
  in-flight remote-job cache.

Bodies take :class:`OffloaderController` as the first arg;
the controller keeps the five ``_on_offloader_*`` methods as
thin bound-method delegates so the
``self._db.bus.add_listener(EventType.X, self._on_x)``
registrations in :meth:`OffloaderController.start` continue
to resolve and tests can instance-call them.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ...helpers.event_bus import Event
from ...models import (
    PAIRING_VERSION_MAX_LEN,
    OffloaderJobStateChangedData,
    OffloaderPairPinMismatchData,
    OffloaderPeerLinkClosedData,
    OffloaderPeerLinkOpenedData,
    OffloaderPinMismatchAlert,
    OffloaderQueueStatusChangedData,
    OffloaderRemoteJobSnapshotEntry,
    PeerQueueStatusSnapshotEntry,
)

if TYPE_CHECKING:
    from .offloader import OffloaderController

# Terminal status set for the offloader-side remote-job cache
# drop-on-terminal logic.
_OFFLOADER_REMOTE_JOB_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)


def on_offloader_pair_pin_mismatch(
    controller: OffloaderController, event: Event[OffloaderPairPinMismatchData]
) -> None:
    """
    Cache the alert in ``_offloader_alerts`` for late-subscriber snapshot.

    Keyed on ``pin_sha256`` (matches the synchronous
    mutation site in :meth:`_apply_pair_status_result`).
    The alert payload adds ``kind`` + ``fired_at`` to the
    bus event's wire fields so the snapshot row survives
    the event drop.
    """
    data = event.data
    # Build the typed alert explicitly rather than as a bare
    # dict literal: ``_offloader_alerts`` is typed
    # ``dict[..., OffloaderAlertSnapshotEntry]`` (a union of
    # ``OffloaderPinMismatchAlert`` / ``OffloaderPeerRevokedAlert``
    # discriminated by ``kind``), and a bare literal under
    # strict mypy can fall back to ``dict[str, object]``
    # rather than narrowing into the right TypedDict variant.
    alert: OffloaderPinMismatchAlert = {
        "kind": "pin_mismatch",
        "receiver_hostname": data["receiver_hostname"],
        "receiver_port": data["receiver_port"],
        "pin_sha256": data["pin_sha256"],
        "receiver_label": data["receiver_label"],
        "expected_pin": data["expected_pin"],
        "observed_pin": data["observed_pin"],
        "fired_at": time.time(),
    }
    controller.state.offloader_alerts[data["pin_sha256"]] = alert


def on_offloader_peer_link_opened(
    controller: OffloaderController, event: Event[OffloaderPeerLinkOpenedData]
) -> None:
    """
    Add ``pin_sha256`` to ``_open_peer_links`` and refresh the receiver version.

    Receiver's ``esphome_version`` rides on every
    ``intent_response`` so a receiver upgrade picks up on
    next session-open without operator action.
    ``pick_build_path``'s deferred version-compat gate reads
    this field.

    Empty / oversize versions are dropped silently rather
    than clobbering — empty would lose the captured value
    after a reconnect from a pre-feature receiver; oversize
    is defense-in-depth against the
    :data:`PAIRING_VERSION_MAX_LEN` cap that the storage
    validator enforces on disk-load.
    """
    data = event.data
    pin_sha256 = data["pin_sha256"]
    controller.state.open_peer_links.add(pin_sha256)
    version = data["esphome_version"]
    if not version or len(version) > PAIRING_VERSION_MAX_LEN:
        return
    pairing = controller.state.pairings.get(pin_sha256)
    if pairing is None or pairing.esphome_version == version:
        return
    pairing.esphome_version = version
    controller._schedule_pairings_save()


def on_offloader_peer_link_closed(
    controller: OffloaderController, event: Event[OffloaderPeerLinkClosedData]
) -> None:
    """Discard ``pin_sha256`` from ``_open_peer_links`` on session close."""
    controller.state.open_peer_links.discard(event.data["pin_sha256"])


def on_offloader_queue_status_changed(
    controller: OffloaderController, event: Event[OffloaderQueueStatusChangedData]
) -> None:
    """Update the offloader-side ``_peer_queue_status`` cache from a wire event."""
    data = event.data
    controller.state.peer_queue_status[data["pin_sha256"]] = PeerQueueStatusSnapshotEntry(
        receiver_hostname=data["receiver_hostname"],
        receiver_port=data["receiver_port"],
        pin_sha256=data["pin_sha256"],
        idle=data["idle"],
        running=data["running"],
        queue_depth=data["queue_depth"],
    )


def on_offloader_job_state_changed(
    controller: OffloaderController, event: Event[OffloaderJobStateChangedData]
) -> None:
    """
    Maintain the offloader-side in-flight remote-job cache.

    Upserts the entry on ``queued`` / ``running``; drops on
    terminal (``completed`` / ``failed`` / ``cancelled``)
    so the snapshot only ever carries actively-running
    rows. The :class:`PeerLinkClient` receive loop already
    validated the wire shape before firing this event.
    """
    data = event.data
    if data["status"] in _OFFLOADER_REMOTE_JOB_TERMINAL_STATUSES:
        controller.state.offloader_remote_jobs.pop(data["job_id"], None)
        return
    controller.state.offloader_remote_jobs[data["job_id"]] = OffloaderRemoteJobSnapshotEntry(
        receiver_hostname=data["receiver_hostname"],
        receiver_port=data["receiver_port"],
        pin_sha256=data["pin_sha256"],
        job_id=data["job_id"],
        status=data["status"],
        error_message=data["error_message"],
    )
