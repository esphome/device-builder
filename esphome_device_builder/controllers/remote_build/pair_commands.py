"""
Offloader-side pair-flow WS commands.

Initiator commands that open Noise XX WebSockets to a
receiver's peer-link endpoint:

- ``preview_pair`` — read-only handshake to capture the
  receiver's pin for OOB verification.
- ``request_pair`` — handshake + ``intent="pair_request"``
  + persist a local :class:`StoredPairing` row.
- ``unpair`` — drop the local row + tear down listeners.
- ``edit_pairing_endpoint`` — user-driven analog of the
  mDNS auto-rebind for receivers the auto path can't catch.
- ``set_pairing_enabled`` — flip the per-pairing
  auto-routing toggle without unpairing.

Bodies take :class:`OffloaderController` as the first arg;
the controller keeps the five ``@api_command``-decorated WS
methods as thin bound-method delegates so test call-sites
and the WS dispatch resolve unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import (
    ErrorCode,
    EventType,
    IntentResponse,
    OffloaderPairingEnabledChangedData,
    PairingSummary,
    PeerStatus,
    StoredPairing,
)
from ._mdns import endpoints_equal
from ._models import EDIT_PAIRING_PROBE_ERRORS, RebindProbeOutcome
from ._validators import (
    HostFieldContext,
    PairLabelField,
    enforce_pin_match,
    intent_response_to_command_error,
    validate_bool,
    validate_hostname,
    validate_pair_label,
    validate_pin_sha256,
    validate_port,
)
from .peer_link_client import PeerLinkClientError
from .peer_link_client import preview_pair as peer_link_preview_pair
from .peer_link_client import request_pair as peer_link_request_pair

if TYPE_CHECKING:
    from .offloader import OffloaderController


async def set_pairing_enabled(
    controller: OffloaderController, *, pin_sha256: str, enabled: bool
) -> PairingSummary:
    """
    Flip the per-pairing enable switch for transparent install.

    Distinct from ``unpair`` — the row stays in
    ``_pairings``, peer-link client keeps its session open,
    the manual-dispatch surface still works. Disables only
    the auto-routing in ``pick_build_path``.

    Unknown pin raises ``NOT_FOUND``. Fires
    ``OFFLOADER_PAIRING_ENABLED_CHANGED`` for cross-tab
    sync and debounce-saves the pairings store.
    """
    clean_pin = validate_pin_sha256(pin_sha256)
    clean_enabled = validate_bool(
        enabled, command="remote_build/set_pairing_enabled", field="enabled"
    )
    pairing = controller.state.pairings.get(clean_pin)
    if pairing is None:
        msg = f"remote_build/set_pairing_enabled: no pairing for pin_sha256={clean_pin!r}"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    pairing.enabled = clean_enabled
    payload: OffloaderPairingEnabledChangedData = {
        "pin_sha256": clean_pin,
        "enabled": clean_enabled,
    }
    controller._db.bus.fire(EventType.OFFLOADER_PAIRING_ENABLED_CHANGED, payload)
    controller._schedule_pairings_save()
    return controller._pairing_summary_for(pairing)


async def preview_pair(
    controller: OffloaderController, *, hostname: str, port: int
) -> dict[str, str]:
    """
    Open a brief Noise XX WS to *hostname*:*port* and return the receiver's pin.

    ``intent="preview"`` captures the receiver's static
    X25519 pubkey from the handshake transcript. The
    frontend renders the returned ``pin_sha256`` for the
    user to OOB-verify against the receiver's "Build
    server" Settings card before calling ``request_pair``.

    Returns ``{"pin_sha256": "<lowercase-hex-64>"}``.
    """
    clean_host = validate_hostname(hostname, context=HostFieldContext.RECEIVER)
    clean_port = validate_port(port, context=HostFieldContext.RECEIVER)
    identity = await controller._db.peer_link_identity_store.async_load()
    try:
        pin = await peer_link_preview_pair(
            hostname=clean_host,
            port=clean_port,
            identity_priv=identity.private_bytes,
            resolver=controller.state.peer_link_resolver,
        )
    except PeerLinkClientError as exc:
        raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
    return {"pin_sha256": pin}


async def request_pair(
    controller: OffloaderController,
    *,
    hostname: str,
    port: int,
    pin_sha256: str,
    receiver_label: str,
    offloader_label: str,
) -> PairingSummary:
    """
    Open a Noise XX WS, send ``intent="pair_request"``, persist a local row.

    Sends ``{"label": offloader_label, "dashboard_id":
    <ours>}`` in the encrypted msg3; the receiver's response
    decides what state the local :class:`StoredPairing` row
    lands in.

    Two labels: *receiver_label* is the offloader-side
    display name (stored locally, never sent); *offloader_label*
    is the offloader's self-identification sent to the
    receiver so its Pairing requests inbox shows a friendly
    name.

    TOCTOU defense: the *pin_sha256* arg is compared against
    the receiver's actual pubkey from the live handshake; a
    mismatch (rotation or MITM) returns
    ``PRECONDITION_FAILED`` and persists nothing.

    Only APPROVED rows reach disk. PENDING lives in-memory
    for the offloader process's lifetime; a restart drops
    them and the user re-runs ``request_pair``.
    """
    clean_host = validate_hostname(hostname, context=HostFieldContext.RECEIVER)
    clean_port = validate_port(port, context=HostFieldContext.RECEIVER)
    clean_pin = validate_pin_sha256(pin_sha256)
    clean_receiver_label = validate_pair_label(receiver_label, field=PairLabelField.RECEIVER_LABEL)
    clean_offloader_label = validate_pair_label(
        offloader_label, field=PairLabelField.OFFLOADER_LABEL
    )
    peer_link_identity, dashboard_identity = await controller._load_offloader_identities_async()

    try:
        result = await peer_link_request_pair(
            hostname=clean_host,
            port=clean_port,
            identity_priv=peer_link_identity.private_bytes,
            label=clean_offloader_label,
            dashboard_id=dashboard_identity.dashboard_id,
            resolver=controller.state.peer_link_resolver,
        )
    except PeerLinkClientError as exc:
        raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc

    enforce_pin_match(expected=clean_pin, observed=result.pin_sha256)
    if (err := intent_response_to_command_error(result.status)) is not None:
        raise err
    if result.status not in (IntentResponse.PENDING, IntentResponse.APPROVED):
        msg = f"unexpected receiver intent_response={result.status.value!r}"
        raise CommandError(ErrorCode.INTERNAL_ERROR, msg)

    # APPROVED here means the receiver short-circuited the
    # inbox dance (re-pair against a still-APPROVED row).
    target_status = (
        PeerStatus.APPROVED if result.status is IntentResponse.APPROVED else PeerStatus.PENDING
    )
    pairing = StoredPairing(
        receiver_hostname=clean_host,
        receiver_port=clean_port,
        pin_sha256=result.pin_sha256,
        static_x25519_pub=result.remote_static_pub,
        label=clean_receiver_label,
        paired_at=time.time(),
        status=target_status,
    )
    key = result.pin_sha256
    # Carry operator-set fields forward across a re-pair of the
    # same receiver identity (same pin). ``enabled`` never
    # self-heals — overwriting it would silently re-enable
    # transparent-install routing the operator turned off.
    # ``esphome_version`` keeps the last-known display value
    # until the next session-open refreshes it.
    prior = controller.state.pairings.get(key)
    if prior is not None:
        pairing.enabled = prior.enabled
        pairing.esphome_version = prior.esphome_version
    # Sweep any stale entry at the same endpoint under a
    # different pin (rotation, or a different receiver took
    # the hostname) so the old row's listener + alert don't
    # orphan under pin-keying.
    controller._sweep_stale_pairings_at_endpoint(clean_host, clean_port, keep_pin_sha256=key)
    # Cancel any prior listener for the same pin — its
    # closure captured the old pairing reference.
    controller.state.pairings[key] = pairing
    controller._cancel_pair_status_listener(key)
    controller._dismiss_offloader_alert(key, clean_host, clean_port)
    if target_status is PeerStatus.APPROVED:
        controller._schedule_pairings_save()
        controller._spawn_peer_link_client(pairing)
    else:
        controller._spawn_pair_status_listener(pairing)
    # Announce the created row so connected tabs that didn't issue
    # this command build it from the event — OFFLOADER_PAIR_STATUS_CHANGED
    # only marks later flips of an already-known row.
    summary = controller._pairing_summary_for(pairing)
    controller._fire_offloader_pairing_added(summary)
    return summary


async def unpair(controller: OffloaderController, *, pin_sha256: str) -> dict[str, bool]:
    """
    Drop the local :class:`StoredPairing` row keyed on *pin_sha256*.

    Idempotent — returns ``{"removed": False}`` rather than
    raising on a missing row so the frontend's Unpair button
    always succeeds visually.

    Receiver-side state is **not** notified; the receiver's
    :class:`StoredPeer` row sticks until the receiver's admin
    clicks Remove. The next ``peer_link`` from this offloader
    returns ``REJECTED`` because our local row is gone.

    In-flight pair-status / peer-link tasks for this pin are
    cancelled before mutating the dict so their open Noise WS
    closes promptly.
    """
    key = validate_pin_sha256(pin_sha256)

    # Cancel before mutating the dict so open Noise WSs close
    # promptly. Idempotent on absent keys.
    controller._cancel_pair_status_listener(key)
    controller._cancel_peer_link_client(key)
    previous = controller.state.pairings.pop(key, None)
    if previous is None:
        return {"removed": False}
    controller._schedule_pairings_save()
    controller._fire_offloader_pair_status_changed(
        previous.receiver_hostname, previous.receiver_port, key, "removed"
    )
    controller._dismiss_offloader_alert(key, previous.receiver_hostname, previous.receiver_port)
    # Drop derived per-peer caches so the snapshot doesn't
    # surface stale data for a row the user just removed.
    controller.state.peer_queue_status.pop(key, None)
    for job_id, entry in list(controller.state.offloader_remote_jobs.items()):
        if entry["pin_sha256"] == key:
            controller.state.offloader_remote_jobs.pop(job_id, None)
    controller.state.open_peer_links.discard(key)
    return {"removed": True}


async def edit_pairing_endpoint(
    controller: OffloaderController,
    *,
    pin_sha256: str,
    hostname: str,
    port: int,
) -> PairingSummary:
    """
    Manually rebind *pin_sha256*'s pairing onto new (*hostname*, *port*) coords.

    For cases the auto path can't catch: cross-subnet
    receivers, mDNS disabled, receiver moved to a
    non-broadcast hostname.

    A one-shot ``preview_pair`` probe verifies the new
    endpoint answers with the same pin
    :class:`StoredPairing` was paired against. Pin mismatch
    deliberately doesn't fall through — accepting a new
    identity under the user's existing trust is what the
    re-auth wizard exists for.

    Returns the updated :class:`PairingSummary`;
    ``connected`` typically reads ``False`` because the
    respawned :class:`PeerLinkClient` is still handshaking
    when this method returns.
    """
    pin = validate_pin_sha256(pin_sha256)
    clean_host = validate_hostname(hostname, context=HostFieldContext.RECEIVER)
    clean_port = validate_port(port, context=HostFieldContext.RECEIVER)

    pairing = controller.state.pairings.get(pin)
    if pairing is None:
        msg = f"edit_pairing_endpoint: no pairing for pin_sha256={pin!r}"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    if pairing.status is not PeerStatus.APPROVED:
        msg = f"edit_pairing_endpoint: pairing status is {pairing.status.value!r}, not APPROVED"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    # System-readiness before user-input semantics: surface
    # "identity not loaded yet" distinctly rather than a
    # confusing "matches current" on a startup race.
    if controller.state.offloader_peer_link_priv is None:
        msg = "edit_pairing_endpoint: offloader peer-link identity not loaded yet"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    if endpoints_equal(pairing.receiver_hostname, pairing.receiver_port, clean_host, clean_port):
        msg = f"edit_pairing_endpoint: new endpoint matches current ({clean_host}:{clean_port})"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)

    result = await controller._probe_pairing_endpoint(
        pairing=pairing, new_hostname=clean_host, new_port=clean_port
    )
    if result.outcome is not RebindProbeOutcome.OK:
        code, template = EDIT_PAIRING_PROBE_ERRORS[result.outcome]
        raise CommandError(
            code,
            template.format(
                host=clean_host,
                port=clean_port,
                pin=pin,
                observed=result.observed_pin,
                error=result.transport_error,
            ),
        )
    controller._commit_endpoint_rebind(pairing, hostname=clean_host, port=clean_port)
    return controller._pairing_summary_for(pairing)
