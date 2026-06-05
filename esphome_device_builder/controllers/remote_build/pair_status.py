"""
Offloader-side pair-status listener for PENDING pairings.

Each PENDING :class:`StoredPairing` gets a long-lived listener
task holding an open Noise WS to its receiver with
``intent="pair_status"``. The receiver-side responder waits on
its own bus event for the admin's accept / reject click and
pushes the response back, so the offloader sees the flip with
sub-second latency without polling. This module owns the spawn
/ cancel lifecycle, the listener loop body, the apply branch
that promotes (or drops) the pairing on the response, and the
three ``OFFLOADER_PAIR_*`` event-fire helpers the apply branch
uses.

Bodies take :class:`OffloaderController` as the first arg; the
controller keeps thin bound-method delegates so test
call-sites (``offloader._await_pair_status_flip``,
``_apply_pair_status_result``, …) and cross-module callers
(``request_pair`` / ``unpair`` /
:func:`peer_link_lifecycle.sweep_stale_pairings_at_endpoint`)
intercept at the controller surface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Literal

from ...models import (
    EventType,
    IntentResponse,
    OffloaderPairingAddedData,
    OffloaderPairPeerRevokedData,
    OffloaderPairPinMismatchData,
    OffloaderPairStatusChangedData,
    OffloaderPeerRevokedAlert,
    OffloaderPinMismatchAlert,
    PairingSummary,
    PeerStatus,
    StoredPairing,
)
from .peer_link_client import PairStatusResult, PeerLinkClientError
from .peer_link_client import await_pair_status as peer_link_await_pair_status

if TYPE_CHECKING:
    from .offloader import OffloaderController

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff for a pair-status listener whose Noise WS
# died on transport error — bounds tight-looping against a
# hard-down receiver.
_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS = 2.0


def spawn_pair_status_listener(controller: OffloaderController, pairing: StoredPairing) -> None:
    """Spawn the pair-status listener task for *pairing* if not already running."""
    key = pairing.pin_sha256
    existing = controller.state.pair_status_listeners.get(key)
    if existing is not None and not existing.done():
        return
    controller.state.pair_status_listeners[key] = asyncio.create_task(
        controller._await_pair_status_flip(pairing),
        name=f"pair-status-{pairing.receiver_hostname}:{pairing.receiver_port}",
    )


def cancel_pair_status_listener(controller: OffloaderController, pin_sha256: str) -> None:
    """Cancel the listener for *pin_sha256*. No-op if none running."""
    task = controller.state.pair_status_listeners.pop(pin_sha256, None)
    if task is not None and not task.done():
        task.cancel()


async def await_pair_status_flip(controller: OffloaderController, pairing: StoredPairing) -> None:
    """Hold a Noise WS to the receiver until the row flips status.

    Single-shot: opens one Noise WS with ``intent="pair_status"``,
    awaits the receiver's response (which the receiver-side
    responder holds open until its own bus fires
    ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` for the matching
    ``dashboard_id``), persists the result + fires
    ``OFFLOADER_PAIR_STATUS_CHANGED``, then exits. On transport
    error, sleeps :data:`_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS`
    and reconnects.
    """
    peer_link_identity, dashboard_identity = await controller._load_offloader_identities_async()
    try:
        while True:
            try:
                result = await peer_link_await_pair_status(
                    hostname=pairing.receiver_hostname,
                    port=pairing.receiver_port,
                    identity_priv=peer_link_identity.private_bytes,
                    dashboard_id=dashboard_identity.dashboard_id,
                    resolver=controller.state.peer_link_resolver,
                )
            except PeerLinkClientError as exc:
                _LOGGER.debug(
                    "pair-status listener for %s:%s reconnecting: %s",
                    pairing.receiver_hostname,
                    pairing.receiver_port,
                    exc,
                )
                await asyncio.sleep(_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS)
                continue
            terminal = await controller._apply_pair_status_result(pairing, result)
            if terminal:
                return
            # Non-terminal result reached the apply path —
            # only happens on a misbehaving receiver returning
            # an unexpected ``intent_response`` (PENDING / OK /
            # NO_PAIRING_WINDOW from a `pair_status` query
            # shouldn't happen). Back off before reconnecting
            # so a bug in the receiver doesn't burn CPU /
            # spam logs in a tight reconnect loop.
            await asyncio.sleep(_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS)
    finally:
        # Only clear the slot if it still points at this task.
        # On a re-pair, ``_cancel_pair_status_listener`` has
        # already popped this task and ``_spawn_pair_status_listener``
        # has put the replacement in the slot — blindly
        # ``pop()``-ing here would evict the replacement and
        # orphan it (no entry left for ``unpair`` to cancel,
        # the new listener parks forever).
        key = pairing.pin_sha256
        if controller.state.pair_status_listeners.get(key) is asyncio.current_task():
            del controller.state.pair_status_listeners[key]


async def apply_pair_status_result(
    controller: OffloaderController, pairing: StoredPairing, result: PairStatusResult
) -> bool:
    """Apply a pair-status response. Return True when the listener should exit.

    * APPROVED + matching pin → flip the row to APPROVED.
    * APPROVED + drifted pin → drop the row (peer-revoked;
      new pubkey under existing trust requires re-pair).
    * REJECTED → drop the row (admin rejected, window
      closed, offloader rotated, or row never existed).
    * Anything else → log + reconnect.

    Race-safe against ``unpair``: every branch keys on
    ``controller.state.pairings.pop(key, None)``, so if the user
    unpaired between the await and this branch we skip
    promotion + event-firing silently.
    """
    host = pairing.receiver_hostname
    port = pairing.receiver_port
    # Captured before the dict mutates — alerts fire
    # alongside ``status="removed"`` and need the label.
    label = pairing.label
    stored_pin = pairing.pin_sha256
    key = pairing.pin_sha256
    if result.status is IntentResponse.APPROVED:
        if result.pin_sha256 != pairing.pin_sha256:
            _LOGGER.warning(
                "pair-status pin drift for %s:%s; dropping row (stored=%s observed=%s)",
                host,
                port,
                pairing.pin_sha256,
                result.pin_sha256,
            )
            if controller.state.pairings.pop(key, None) is not None:
                controller._schedule_pairings_save()
                pin_alert: OffloaderPinMismatchAlert = {
                    "kind": "pin_mismatch",
                    "receiver_hostname": host,
                    "receiver_port": port,
                    "pin_sha256": stored_pin,
                    "receiver_label": label,
                    "expected_pin": stored_pin,
                    "observed_pin": result.pin_sha256,
                    "fired_at": time.time(),
                }
                controller.state.offloader_alerts[key] = pin_alert
                # Fire diagnostic first so subscribers see
                # the full payload before the row drops.
                _fire_offloader_pair_pin_mismatch(
                    controller, host, port, key, label, stored_pin, result.pin_sha256
                )
                controller._fire_offloader_pair_status_changed(host, port, key, "removed")
            return True
        # PENDING → APPROVED in place. If ``unpair`` raced
        # us between the await and this branch the row's
        # gone; exit silently rather than resurrect state
        # the user just deleted.
        existing = controller.state.pairings.get(key)
        if existing is None:
            return True
        existing.status = PeerStatus.APPROVED
        controller._schedule_pairings_save()
        controller._fire_offloader_pair_status_changed(host, port, key, "approved")
        controller._spawn_peer_link_client(existing)
        return True
    if result.status is IntentResponse.REJECTED:
        if controller.state.pairings.pop(key, None) is not None:
            controller._schedule_pairings_save()
            revoked_alert: OffloaderPeerRevokedAlert = {
                "kind": "peer_revoked",
                "receiver_hostname": host,
                "receiver_port": port,
                "pin_sha256": stored_pin,
                "receiver_label": label,
                "fired_at": time.time(),
            }
            controller.state.offloader_alerts[key] = revoked_alert
            _fire_offloader_pair_peer_revoked(controller, host, port, key, label)
            controller._fire_offloader_pair_status_changed(host, port, key, "removed")
        return True
    _LOGGER.warning(
        "pair-status returned unexpected status %r for %s:%s",
        result.status,
        host,
        port,
    )
    return False


def fire_offloader_pair_status_changed(
    controller: OffloaderController,
    receiver_hostname: str,
    receiver_port: int,
    pin_sha256: str,
    status: Literal["approved", "removed"],
) -> None:
    """Fire ``OFFLOADER_PAIR_STATUS_CHANGED`` for a pairing flip."""
    payload: OffloaderPairStatusChangedData = {
        "receiver_hostname": receiver_hostname,
        "receiver_port": receiver_port,
        "pin_sha256": pin_sha256,
        "status": status,
    }
    controller._db.bus.fire(EventType.OFFLOADER_PAIR_STATUS_CHANGED, payload)


def fire_offloader_pairing_added(
    controller: OffloaderController,
    summary: PairingSummary,
) -> None:
    """Fire ``OFFLOADER_PAIRING_ADDED`` so other tabs build the new row."""
    status: Literal["pending", "approved"] = (
        "approved" if summary.status is PeerStatus.APPROVED else "pending"
    )
    payload: OffloaderPairingAddedData = {
        "receiver_hostname": summary.receiver_hostname,
        "receiver_port": summary.receiver_port,
        "pin_sha256": summary.pin_sha256,
        "label": summary.label,
        "paired_at": summary.paired_at,
        "status": status,
        "connected": summary.connected,
        "connecting": summary.connecting,
        "last_connect_error": summary.last_connect_error,
        "esphome_version": summary.esphome_version,
        "enabled": summary.enabled,
    }
    controller._db.bus.fire(EventType.OFFLOADER_PAIRING_ADDED, payload)


def _fire_offloader_pair_pin_mismatch(
    controller: OffloaderController,
    receiver_hostname: str,
    receiver_port: int,
    pin_sha256: str,
    receiver_label: str,
    expected_pin: str,
    observed_pin: str,
) -> None:
    """Fire ``OFFLOADER_PAIR_PIN_MISMATCH`` for a drifted-pin pair_status."""
    payload: OffloaderPairPinMismatchData = {
        "receiver_hostname": receiver_hostname,
        "receiver_port": receiver_port,
        "receiver_label": receiver_label,
        "pin_sha256": pin_sha256,
        "expected_pin": expected_pin,
        "observed_pin": observed_pin,
    }
    controller._db.bus.fire(EventType.OFFLOADER_PAIR_PIN_MISMATCH, payload)


def _fire_offloader_pair_peer_revoked(
    controller: OffloaderController,
    receiver_hostname: str,
    receiver_port: int,
    pin_sha256: str,
    receiver_label: str,
) -> None:
    """Fire ``OFFLOADER_PAIR_PEER_REVOKED`` for a REJECTED pair_status."""
    payload: OffloaderPairPeerRevokedData = {
        "receiver_hostname": receiver_hostname,
        "receiver_port": receiver_port,
        "receiver_label": receiver_label,
        "pin_sha256": pin_sha256,
    }
    controller._db.bus.fire(EventType.OFFLOADER_PAIR_PEER_REVOKED, payload)
