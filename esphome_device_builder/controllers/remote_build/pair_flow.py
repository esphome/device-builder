"""Receiver-side peer-link Noise WS dispatch helpers (pair flow)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Literal

from ...helpers.event_bus import Event
from ...models import (
    EventType,
    IntentResponse,
    RemoteBuildPairRequestReceivedData,
    RemoteBuildPairStatusChangedData,
    StoredPeer,
)

if TYPE_CHECKING:
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)


async def record_pair_request(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
    static_x25519_pub: bytes,
    label: str,
    peer_ip: str,
) -> IntentResponse:
    """
    Process an ``intent="pair_request"`` Noise session.

    Returns:
    * ``APPROVED`` — row exists for ``dashboard_id`` with
      APPROVED status and matching pin. Re-pair against
      existing trust bypasses the pairing window so an
      offloader hiccup doesn't force a re-approve.
    * ``PENDING`` — new ``StoredPeer`` created or existing
      PENDING row refreshed. Only reachable inside an open
      pairing window; fires
      :attr:`EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED`
      so the receiver UI surfaces the inbox row.
    * ``REJECTED`` — APPROVED row exists but pin doesn't
      match: offloader rotated identity, or someone is
      claiming a stranger's ``dashboard_id``. Refused
      regardless of window state.
    * ``NO_PAIRING_WINDOW`` — closed window for a request
      that would create/refresh a PENDING row.
    """
    # Already-APPROVED row: re-pair against existing trust
    # bypasses the window. Pin mismatch is refused regardless
    # (rotation or impersonation).
    approved_peer = controller.state.approved_peers.get(dashboard_id)
    if approved_peer is not None:
        if approved_peer.pin_sha256 != pin_sha256:
            return IntentResponse.REJECTED
        return IntentResponse.APPROVED

    if not controller.is_pairing_window_open():
        return IntentResponse.NO_PAIRING_WINDOW

    # Refuse to overwrite a PENDING entry's pubkey — defense
    # in depth against a LAN attacker injecting a rival key
    # under the same scraped dashboard_id (the OOB fingerprint
    # check at approve-time is the load-bearing gate, but
    # silent overwrite enables a DoS). Same-pubkey retries
    # refresh label / peer_ip / paired_at via the path below.
    existing = controller.state.pending_peers.get(dashboard_id)
    if existing is not None and existing.static_x25519_pub != static_x25519_pub:
        _LOGGER.warning(
            "pair_request from %s claims dashboard_id=%s but presented "
            "a different X25519 pubkey than the existing PENDING entry "
            "from %s; refusing the overwrite",
            peer_ip,
            dashboard_id,
            existing.peer_ip,
        )
        return IntentResponse.REJECTED

    paired_at = time.time()
    controller.state.pending_peers[dashboard_id] = StoredPeer(
        dashboard_id=dashboard_id,
        pin_sha256=pin_sha256,
        static_x25519_pub=static_x25519_pub,
        label=label,
        paired_at=paired_at,
        peer_ip=peer_ip,
    )
    payload: RemoteBuildPairRequestReceivedData = {
        "dashboard_id": dashboard_id,
        "pin_sha256": pin_sha256,
        "label": label,
        "peer_ip": peer_ip,
        "paired_at": paired_at,
    }
    controller._db.bus.fire(EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED, payload)
    return IntentResponse.PENDING


async def lookup_peer_for_session(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
) -> IntentResponse:
    """
    Resolve an ``intent="peer_link"`` request.

    Returns ``OK`` if APPROVED + pin matches, ``PENDING`` if
    the row's still in the pending dict (admin hasn't clicked
    Accept), ``REJECTED`` for no row or pin drift. The
    offloader treats REJECTED as "send a fresh pair_request".
    """
    return await _lookup_peer_response(
        controller,
        dashboard_id=dashboard_id,
        pin_sha256=pin_sha256,
        approved_response=IntentResponse.OK,
    )


async def lookup_peer_for_status(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
) -> IntentResponse:
    """
    Resolve an ``intent="pair_status"`` query, long-polling on PENDING.

    Returns :attr:`IntentResponse.APPROVED` or ``REJECTED``.
    REJECTED is reached four ways: never paired, admin
    clicked Reject, offloader's peer-link identity rotated,
    or window-close cleared the pending dict mid-wait. The
    offloader treats all of them as peer-revoked.

    Long-poll: with snapshot=PENDING, await
    :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` for
    the matching ``dashboard_id``. No timeout — WS hangs
    until the offloader cancels or the dict mutates.

    Listener-attach-before-snapshot ordering is
    load-bearing: an ``approve_peer`` firing between
    snapshot and wait must not slip past. Window-gating is
    implicit — closed window = empty pending dict = REJECTED
    on snapshot, long-poll never starts.

    Differs from :func:`lookup_peer_for_session` only in
    returning ``APPROVED`` vs ``OK`` — pair_status is
    informational, peer_link is connection-establishing.
    """
    flip_event = asyncio.Event()

    def _on_pair_status(event: Event[RemoteBuildPairStatusChangedData]) -> None:
        if event.data["dashboard_id"] == dashboard_id:
            flip_event.set()

    with controller._db.bus.listening(
        [EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED], _on_pair_status
    ):
        snapshot = await _lookup_peer_response(
            controller,
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            approved_response=IntentResponse.APPROVED,
        )
        if snapshot is not IntentResponse.PENDING:
            return snapshot
        await flip_event.wait()
        return await _lookup_peer_response(
            controller,
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            approved_response=IntentResponse.APPROVED,
        )


def fire_pair_status_changed(
    controller: ReceiverController,
    dashboard_id: str,
    status: Literal["approved", "removed"],
) -> None:
    """Fire ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` for a peer transition."""
    payload: RemoteBuildPairStatusChangedData = {
        "dashboard_id": dashboard_id,
        "status": status,
    }
    controller._db.bus.fire(EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED, payload)


async def _lookup_peer_response(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
    approved_response: IntentResponse,
) -> IntentResponse:
    """
    Shared lookup core for the peer_link / pair_status WS dispatch paths.

    Walks the in-memory PENDING dict first, then the persisted
    APPROVED list. Both intents need the same pin-match check
    on either store; only the APPROVED return value differs
    (caller passes :attr:`IntentResponse.OK` for peer_link,
    :attr:`IntentResponse.APPROVED` for pair_status).

    Returns ``REJECTED`` when no row matches OR pin doesn't
    match — the offloader treats either case the same (drop
    local row + surface re-pair UI).
    """
    # PENDING dict first — most pair-flow traffic is pending
    # peers polling pair_status. Both lookups are RAM reads
    # (the APPROVED list moved off disk into
    # ``state.approved_peers`` at startup).
    pending = controller.state.pending_peers.get(dashboard_id)
    if pending is not None:
        if pending.pin_sha256 != pin_sha256:
            return IntentResponse.REJECTED
        return IntentResponse.PENDING
    peer = controller.state.approved_peers.get(dashboard_id)
    if peer is None or peer.pin_sha256 != pin_sha256:
        return IntentResponse.REJECTED
    return approved_response
