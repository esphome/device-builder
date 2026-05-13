"""Receiver-side TypedDict event payloads fired on the dashboard event bus."""

from __future__ import annotations

from typing import Literal, TypedDict


class RemoteBuildIdentityRotatedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_IDENTITY_ROTATED``.

    Fires after ``rotate_identity`` persists the new X25519
    keypair to disk and *attempts* the listener rebuild — the
    rebuild can fail-soft (port collision, permission denied),
    in which case ``IdentityView.listener_bound`` returns
    ``False``. Subscribers should check ``listener_bound``
    before assuming end-to-end propagation.
    """

    dashboard_id: str
    pin_sha256: str


class RemoteBuildPairRequestReceivedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.

    Fired by the peer-link Noise WS handler when a fresh
    ``intent="pair_request"`` arrives during an open pairing
    window. ``peer_ip`` lets the operator sanity-check the source
    before OOB-confirming the pin; ``paired_at`` lets a subscriber
    rendering the inbox sort most-recent-first without a follow-up
    snapshot read.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    peer_ip: str
    paired_at: float


class RemoteBuildPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED``.

    ``status="approved"`` fires from ``approve_peer``;
    ``status="removed"`` from ``remove_peer`` and from
    pairing-window-close clearing the PENDING dict. The
    ``"removed"`` events wake any in-flight
    ``intent="pair_status"`` long-poll on the matching offloader
    so its listener task drops its local state.
    """

    dashboard_id: str
    status: Literal["approved", "removed"]


class RemoteBuildPairingWindowChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED``.

    Fires whenever the in-process pairing window opens, extends,
    or closes. ``expires_in_seconds`` is ``None`` when ``open`` is
    ``False``; otherwise it's the remaining lifetime against the
    latest extend, which the frontend renders as a live countdown.
    """

    open: bool
    expires_in_seconds: float | None


class ReceiverPeerLinkSessionOpenedData(TypedDict):
    """
    Payload for ``EventType.RECEIVER_PEER_LINK_SESSION_OPENED``.

    Fires from
    :meth:`ReceiverController.register_peer_link_session` once
    the post-handshake dispatch loop is parked. ``dashboard_id``
    is the offloader's stable identity captured from the Noise
    XX handshake transcript.
    """

    dashboard_id: str


class ReceiverPeerLinkSessionClosedData(TypedDict):
    """
    Payload for ``EventType.RECEIVER_PEER_LINK_SESSION_CLOSED``.

    Fires when the receiver's session loop unwinds. No ``reason``
    field — the receiver only sees "the loop returned"; the rich
    transport / heartbeat / terminate classification lives on
    the offloader side where those branches diverge.
    """

    dashboard_id: str
