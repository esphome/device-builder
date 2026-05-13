"""Receiver-side TypedDict event payloads fired on the dashboard event bus."""

from __future__ import annotations

from typing import Literal, TypedDict

# ---------------------------------------------------------------------------
# Event payload shapes (TypedDict so the bus.fire data dict is type-checked at
# the call site without changing the wire shape; mirrors HA's
# ``EventStateChangedData`` / ``EventStateReportedData`` pattern).
# ---------------------------------------------------------------------------


class RemoteBuildIdentityRotatedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_IDENTITY_ROTATED``.

    Fires after ``rotate_identity`` persists the new X25519
    keypair to disk and attempts the listener rebuild. The
    event ONLY signals that the rotation landed on disk and
    the rebuild was attempted — not that the listener is
    currently serving traffic against the new key. The
    rebuild can fail-soft (port collision, permission denied,
    listener unbound at rotation time), in which case the
    rotater's ``IdentityView`` response carries
    ``listener_bound=False`` and the new pin will hit the
    wire on the next successful bind. Subscribers (the
    offloader-side peer-link, the receiver Settings UI) use
    the event to refresh their cached pin without polling
    ``get_identity``; they should check
    ``IdentityView.listener_bound`` (via a follow-up
    ``get_identity`` call or by inspecting their own session
    state) before assuming end-to-end propagation.
    """

    dashboard_id: str
    pin_sha256: str


class RemoteBuildPairRequestReceivedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.

    Fired by the peer-link Noise WS handler when a fresh
    ``intent="pair_request"`` arrives during an open pairing
    window. The receiver Settings UI surfaces the row in the
    Pairing requests inbox; ``peer_ip`` lets the operator
    sanity-check the source against expectations before
    OOB-confirming the pin.

    ``paired_at`` carries the receiver-clock unix timestamp at
    row creation, matching the ``StoredPeer.paired_at`` field
    on the in-memory PENDING entry. Lets a subscriber rendering
    the inbox from the event stream sort the most-recent attempt
    first without a follow-up snapshot read.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    peer_ip: str
    paired_at: float


class RemoteBuildPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED``.

    Fires from three paths:

    * ``approve_peer`` promoting a PENDING dict entry to
      APPROVED (``status="approved"``).
    * ``remove_peer`` dropping either a PENDING dict entry or
      an APPROVED list row (``status="removed"``).
    * Pairing-window-close clearing the in-memory PENDING dict
      (``status="removed"`` per cleared entry).

    The ``status="removed"`` event is what wakes any in-flight
    ``intent="pair_status"`` long-poll on a paired offloader so
    its listener task drops the offloader's local state.
    """

    dashboard_id: str
    status: Literal["approved", "removed"]


class RemoteBuildPairingWindowChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED``.

    Fires whenever the in-process pairing window opens, extends,
    or closes. ``expires_in_seconds`` is ``None`` when ``open`` is
    ``False``; otherwise it's the remaining lifetime against the
    latest extend, which the receiver-side frontend renders as a
    live countdown.
    """

    open: bool
    expires_in_seconds: float | None


class ReceiverPeerLinkSessionOpenedData(TypedDict):
    """
    Payload for ``EventType.RECEIVER_PEER_LINK_SESSION_OPENED``.

    Fired by :meth:`ReceiverController.register_peer_link_session`
    after the receiver has installed an offloader's peer-link
    Noise WS session in its ``_peer_link_sessions`` registry —
    i.e. the post-handshake ``_run_peer_link_session`` is about
    to enter its dispatch loop. Receiver-side counterpart to
    :class:`OffloaderPeerLinkOpenedData`.

    ``dashboard_id`` is the offloader's stable identity captured
    from the Noise XX handshake transcript.
    """

    dashboard_id: str


class ReceiverPeerLinkSessionClosedData(TypedDict):
    """
    Payload for ``EventType.RECEIVER_PEER_LINK_SESSION_CLOSED``.

    Fired by :meth:`ReceiverController.unregister_peer_link_session`
    when the receiver's session loop unwinds (offloader
    disconnect, heartbeat timeout, controller shutdown,
    ``superseded`` eviction). Receiver-side counterpart to
    :class:`OffloaderPeerLinkClosedData` — but no ``reason``
    field, because the receiver only sees "the loop returned"
    and the rich reason classification (transport vs heartbeat
    vs structured terminate) lives on the offloader side where
    those branches diverge.
    """

    dashboard_id: str
