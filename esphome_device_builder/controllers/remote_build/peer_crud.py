"""Receiver-side ``approve_peer`` / ``remove_peer`` WS commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import ErrorCode, RemoteBuildSettingsView
from ._validators import validate_dashboard_id

if TYPE_CHECKING:
    from .receiver import ReceiverController


# Debounce window for the receiver-side peers-store write so a
# burst of approvals collapses to one disk write.
_PEERS_SAVE_DELAY_SECONDS = 1.0


async def approve_peer(
    controller: ReceiverController, *, dashboard_id: str
) -> RemoteBuildSettingsView:
    """
    Promote a PENDING peer to APPROVED.

    Pops the in-memory PENDING entry, inserts it into the
    RAM-canonical ``state.approved_peers`` dict, schedules a
    debounced write to the receiver-peers store, and fires
    :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` with
    ``{dashboard_id, status: "approved"}``. The offloader's
    pair-status listener observes the flip via the bus event +
    re-snapshot path. ``NOT_FOUND`` if no PENDING entry
    matches; ``INVALID_ARGS`` if the dashboard_id already
    corresponds to an APPROVED row (duplicate Accept click,
    almost always a UI race; refuse rather than silently
    re-fire the event).
    """
    clean_id = validate_dashboard_id(dashboard_id)

    pending = controller.state.pending_peers.pop(clean_id, None)
    if pending is None:
        # Differentiate "already approved" from "never existed"
        # so the frontend can decide whether to refresh or
        # surface an error. Both reads short-circuit through
        # RAM — no disk I/O.
        if clean_id in controller.state.approved_peers:
            msg = f"peer is already approved: {clean_id}"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        msg = f"no pending peer with dashboard_id: {clean_id}"
        raise CommandError(ErrorCode.NOT_FOUND, msg)

    controller.state.approved_peers[clean_id] = pending
    controller._peers_store.async_delay_save(
        controller._serialize_peers, delay=_PEERS_SAVE_DELAY_SECONDS
    )
    controller._fire_pair_status_changed(clean_id, "approved")
    return await controller._current_settings_view()


async def remove_peer(
    controller: ReceiverController, *, dashboard_id: str
) -> RemoteBuildSettingsView:
    """
    Delete a peer row (works on both PENDING and APPROVED).

    Two semantically distinct outcomes share the same WS command:

    * Removing a PENDING entry from the in-memory dict is
      *rejection* — the row never represented established
      trust, so this is inbox cleanup. Fires the
      ``status="removed"`` event so any offloader currently
      long-polling pair_status sees the cancellation and
      drops its local state.
    * Removing an APPROVED row from ``state.approved_peers``
      (RAM-canonical, debounced to disk) is *revocation* —
      fires the same
      :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED`
      ``status="removed"`` event so the offloader can
      surface a ``peer_revoked`` UI alert.

    ``NOT_FOUND`` if neither dict has a row.
    """
    clean_id = validate_dashboard_id(dashboard_id)

    # PENDING: in-memory, no disk write needed (PENDING never
    # reaches the peers store).
    if controller.state.pending_peers.pop(clean_id, None) is not None:
        controller._fire_pair_status_changed(clean_id, "removed")
        return await controller._current_settings_view()

    if controller.state.approved_peers.pop(clean_id, None) is None:
        msg = f"no peer with dashboard_id: {clean_id}"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    controller._peers_store.async_delay_save(
        controller._serialize_peers, delay=_PEERS_SAVE_DELAY_SECONDS
    )
    controller._fire_pair_status_changed(clean_id, "removed")
    return await controller._current_settings_view()
