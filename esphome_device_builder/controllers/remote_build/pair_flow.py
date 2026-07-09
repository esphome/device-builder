"""Receiver-side peer-link Noise WS dispatch helpers (pair flow)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ...helpers.event_bus import Event
from ...helpers.pairing_key import pairing_key_matches
from ...models import (
    EventType,
    IntentResponse,
    RejectReason,
    RemoteBuildPairRequestReceivedData,
    RemoteBuildPairStatusChangedData,
    StoredPeer,
)

if TYPE_CHECKING:
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntentOutcome:
    """
    A receiver-side intent decision: the wire response plus an optional reason.

    ``reason`` rides the wire to disambiguate the opaque
    ``REJECTED`` (and marks a not-yet-approved ``PENDING`` on the
    lookup path); the self-describing ``OK`` / ``APPROVED`` /
    ``NO_PAIRING_WINDOW`` responses leave it ``None``.
    """

    response: IntentResponse
    reason: RejectReason | None = None


async def record_pair_request(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
    static_x25519_pub: bytes,
    label: str,
    peer_ip: str,
    pairing_key: str | None = None,
) -> IntentOutcome:
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
    * ``APPROVED`` (auto) — ``state.auto_approve_first_pair``
      armed inside an open window with zero APPROVED rows
      (the ``--remote-build-only`` first-pair bootstrap) and
      *pairing_key* matches ``state.bootstrap_pairing_key``:
      the row lands directly in ``approved_peers``, is flushed
      to disk before the wire response, and the one-shot flag
      disarms.
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
            _LOGGER.warning(
                "pair_request pin mismatch for dashboard_id=%s from %s against an "
                "APPROVED row (stored_offloader_pin=%s observed_offloader_pin=%s); "
                "refusing (offloader identity rotated or dashboard_id impersonation)",
                dashboard_id,
                peer_ip,
                approved_peer.pin_sha256,
                pin_sha256,
            )
            return IntentOutcome(IntentResponse.REJECTED, RejectReason.PIN_MISMATCH)
        return IntentOutcome(IntentResponse.APPROVED)

    if not controller.is_pairing_window_open():
        # Every refusal path logs its reason so a headless operator can
        # tell a closed/lapsed window from a key or source rejection.
        _LOGGER.warning(
            "Refused pair_request from %s (dashboard_id=%s): pairing window is closed "
            "(no window open — already paired, lapsed, or the receiver's Pairing "
            "requests screen isn't open)",
            peer_ip,
            dashboard_id,
        )
        return IntentOutcome(IntentResponse.NO_PAIRING_WINDOW)

    if controller.state.auto_approve_first_pair and not controller.state.approved_peers:
        return await _auto_approve_or_refuse(
            controller,
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            static_x25519_pub=static_x25519_pub,
            label=label,
            peer_ip=peer_ip,
            pairing_key=pairing_key,
        )

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
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.PIN_MISMATCH)

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
    return IntentOutcome(IntentResponse.PENDING)


async def lookup_peer_for_session(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
) -> IntentOutcome:
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
) -> IntentOutcome:
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
        if snapshot.response is not IntentResponse.PENDING:
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


async def _auto_approve_or_refuse(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
    static_x25519_pub: bytes,
    label: str,
    peer_ip: str,
    pairing_key: str | None,
) -> IntentOutcome:
    """
    First-pair window is armed: auto-approve the request, or refuse it.

    Requires *pairing_key* to match ``state.bootstrap_pairing_key``
    (fail closed when no key is armed) and honours
    ``--allow-pairing-source``. Refusals never disarm and return the
    same ``NO_PAIRING_WINDOW`` a closed window does, leaking nothing
    about which gate fired.
    """
    expected_key = controller.state.bootstrap_pairing_key
    if expected_key is None or not pairing_key_matches(expected_key, pairing_key):
        # Name the receiver-side "no key armed" case distinctly from the
        # offloader-side missing/mismatch so the diagnostic points at the
        # right side; the wire response is identical for all three.
        if expected_key is None:
            reason = "no key armed"
        elif not pairing_key:
            reason = "missing"
        else:
            reason = "mismatch"
        _LOGGER.warning(
            "Refused auto-pair from %s (dashboard_id=%s): pairing key %s",
            peer_ip,
            dashboard_id,
            reason,
        )
        return IntentOutcome(IntentResponse.NO_PAIRING_WINDOW)
    if _pairing_source_allowed(controller, peer_ip):
        return await _auto_approve_pair_request(
            controller,
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            static_x25519_pub=static_x25519_pub,
            label=label,
            peer_ip=peer_ip,
        )
    _LOGGER.warning(
        "Refused auto-pair from %s (dashboard_id=%s): source not in "
        "--allow-pairing-source allowlist %s",
        peer_ip,
        dashboard_id,
        controller._db.settings.allow_pairing_sources,
    )
    return IntentOutcome(IntentResponse.NO_PAIRING_WINDOW)


def _pairing_source_allowed(controller: ReceiverController, peer_ip: str) -> bool:
    """
    Whether *peer_ip* may use the first-pair auto-approve window.

    ``True`` when no ``--allow-pairing-source`` allowlist is
    configured (trust-on-first-use, the default). When one is set,
    ``True`` only if *peer_ip* normalises to a listed address; an
    unparseable ``peer_ip`` never matches.
    """
    allowlist = controller._db.settings.allow_pairing_sources
    if not allowlist:
        return True
    try:
        normalized = str(ipaddress.ip_address(peer_ip))
    except ValueError:
        return False
    return normalized in allowlist


async def _auto_approve_pair_request(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
    static_x25519_pub: bytes,
    label: str,
    peer_ip: str,
) -> IntentOutcome:
    """
    First-pair bootstrap: approve the request without the inbox dance.

    Caller has already verified the bootstrap pairing key (and the
    source allowlist, when set). Disarms the one-shot flag *and*
    clears the now-consumed key *before* the store flush so a second
    request arriving during the await can't auto-approve and the key
    doesn't linger in RAM past its single use (the bootstrap runner's
    finally also clears it). The flush is awaited (not debounced) —
    this write is the mode's single trust-establishing state; losing
    it to a crash inside a debounce window would strand an offloader
    that believes it's APPROVED.
    """
    controller.state.auto_approve_first_pair = False
    controller.state.bootstrap_pairing_key = None
    peer = StoredPeer(
        dashboard_id=dashboard_id,
        pin_sha256=pin_sha256,
        static_x25519_pub=static_x25519_pub,
        label=label,
        paired_at=time.time(),
        peer_ip=peer_ip,
    )
    controller.state.approved_peers[dashboard_id] = peer
    controller._peers_store.async_delay_save(controller._serialize_peers)
    await controller._peers_store.async_save_now()
    _LOGGER.info(
        "Auto-approved first pairing: %r (dashboard_id=%s, offloader pin %s) from %s",
        label,
        dashboard_id,
        pin_sha256,
        peer_ip,
    )
    controller._fire_pair_status_changed(dashboard_id, "approved")
    return IntentOutcome(IntentResponse.APPROVED)


async def _lookup_peer_response(
    controller: ReceiverController,
    *,
    dashboard_id: str,
    pin_sha256: str,
    approved_response: IntentResponse,
) -> IntentOutcome:
    """
    Shared lookup core for the peer_link / pair_status WS dispatch paths.

    Walks the in-memory PENDING dict first, then the persisted
    APPROVED list. Both intents need the same pin-match check
    on either store; only the APPROVED return value differs
    (caller passes :attr:`IntentResponse.OK` for peer_link,
    :attr:`IntentResponse.APPROVED` for pair_status).

    Returns ``REJECTED`` (with a :class:`RejectReason`) when no
    row matches OR pin doesn't match; the pin-mismatch branch
    logs the stored vs observed offloader pin.
    """
    # PENDING dict first — most pair-flow traffic is pending
    # peers polling pair_status. Both lookups are RAM reads
    # (the APPROVED list moved off disk into
    # ``state.approved_peers`` at startup).
    pending = controller.state.pending_peers.get(dashboard_id)
    if pending is not None:
        if pending.pin_sha256 != pin_sha256:
            _LOGGER.warning(
                "peer-link pin mismatch for dashboard_id=%s against a PENDING row "
                "(stored_offloader_pin=%s observed_offloader_pin=%s)",
                dashboard_id,
                pending.pin_sha256,
                pin_sha256,
            )
            return IntentOutcome(IntentResponse.REJECTED, RejectReason.PIN_MISMATCH)
        return IntentOutcome(IntentResponse.PENDING, RejectReason.PENDING_NOT_APPROVED)
    peer = controller.state.approved_peers.get(dashboard_id)
    if peer is None:
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.NO_APPROVED_PEER)
    if peer.pin_sha256 != pin_sha256:
        _LOGGER.warning(
            "peer-link pin mismatch for dashboard_id=%s against an APPROVED row "
            "(stored_offloader_pin=%s observed_offloader_pin=%s); offloader identity "
            "rotated or a stranger is claiming this dashboard_id",
            dashboard_id,
            peer.pin_sha256,
            pin_sha256,
        )
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.PIN_MISMATCH)
    return IntentOutcome(approved_response)
