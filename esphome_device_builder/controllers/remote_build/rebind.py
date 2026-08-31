"""
Offloader-side endpoint rebind for stored pairings.

Owns the probe-and-rebind path that keeps an APPROVED
:class:`StoredPairing` row tracking its receiver across
hostname / port moves. Three callers feed in: mDNS
auto-rebind via :func:`maybe_schedule_rebind_probe` (fired
from :mod:`.discovery` on every resolved broadcast), the
user-driven :meth:`OffloaderController.edit_pairing_endpoint`
WS command, and the receiver's ``connect_back`` announce via
:func:`handle_connect_back`. All share the
:func:`probe_pairing_endpoint` identity-verify step.

Bodies take :class:`OffloaderController` as the first arg;
the controller keeps thin bound-method delegates for
``_probe_pairing_endpoint`` / ``_probe_and_rebind_endpoint``
/ ``_commit_endpoint_rebind`` / ``_maybe_schedule_rebind_probe``
so cross-module callers and tests intercept at stable hook
points.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from ...helpers.hostname import normalize_hostname
from ...helpers.ip import is_ip_address, port_or_zero
from ...models import (
    EventType,
    IntentResponse,
    OffloaderPairEndpointReboundData,
    PeerStatus,
    RejectReason,
    RemoteBuildPeer,
    StoredPairing,
)
from ._intent import IntentOutcome
from ._mdns import endpoints_equal
from ._models import RebindProbeOutcome, RebindProbeResult
from .peer_link_client import PeerLinkClientError
from .peer_link_client import preview_pair as peer_link_preview_pair

if TYPE_CHECKING:
    from .offloader import OffloaderController

_LOGGER = logging.getLogger(__name__)

# Per-pin sliding window between mDNS rebind probes. Doubles
# as in-flight guard + retry throttle so a permanently-down
# host doesn't trigger a probe per mDNS Updated burst.
_REBIND_PROBE_COOLDOWN_SECONDS = 30.0


async def probe_pairing_endpoint(
    controller: OffloaderController,
    *,
    pairing: StoredPairing,
    new_hostname: str,
    new_port: int,
) -> RebindProbeResult:
    """Probe + identity-verify a candidate endpoint without mutating state.

    Shared by the mDNS auto-rebind path and the user-driven
    endpoint edit; each caller maps the typed outcome onto
    its own surface. One ``intent="preview"`` round-trip
    covers three checks: reachability (TCP + handshake),
    identity (pubkey vs stored pin), and race-safety
    (captured pairing object still in the dict, still
    APPROVED).
    """
    assert controller.state.offloader_peer_link_priv is not None
    try:
        observed_pin = (
            await peer_link_preview_pair(
                hostname=new_hostname,
                port=new_port,
                identity_priv=controller.state.offloader_peer_link_priv,
                resolver=controller.state.peer_link_resolver,
            )
        ).pin_sha256
    except PeerLinkClientError as exc:
        return RebindProbeResult(RebindProbeOutcome.UNREACHABLE, transport_error=exc)
    if observed_pin != pairing.pin_sha256:
        return RebindProbeResult(RebindProbeOutcome.PIN_MISMATCH, observed_pin=observed_pin)
    current = controller.state.pairings.get(pairing.pin_sha256)
    if current is not pairing:
        return RebindProbeResult(RebindProbeOutcome.PAIRING_REPLACED)
    if current.status is not PeerStatus.APPROVED:
        return RebindProbeResult(RebindProbeOutcome.STATUS_CHANGED)
    return RebindProbeResult(RebindProbeOutcome.OK)


async def commit_endpoint_rebind(
    controller: OffloaderController, pairing: StoredPairing, *, hostname: str, port: int
) -> None:
    """Mutate *pairing* to (*hostname*, *port*) and run the rebind epilogue.

    Clears the per-pin probe cooldown — a successful rebind
    means the next mDNS Updated should probe immediately.
    Caller owns the probe + identity verify; no checks here.
    The rebound event fires only when the endpoint actually
    changed; an unchanged commit still saves and respawns.
    """
    changed = not endpoints_equal(pairing.receiver_hostname, pairing.receiver_port, hostname, port)
    pairing.receiver_hostname = hostname
    pairing.receiver_port = port
    controller._schedule_pairings_save()
    await _respawn_peer_link_at_new_endpoint(controller, pairing, fire_rebound=changed)
    controller.state.rebind_probe_until.pop(pairing.pin_sha256, None)


async def _respawn_peer_link_at_new_endpoint(
    controller: OffloaderController, pairing: StoredPairing, *, fire_rebound: bool
) -> None:
    """Cancel + respawn the peer-link client; fire the rebind event when asked.

    Awaits the old client's teardown first (see
    :func:`peer_link_lifecycle.cancel_peer_link_client_and_wait`). The
    caller has already mutated *pairing*'s hostname/port.
    """
    await controller._cancel_peer_link_client_and_wait(pairing.pin_sha256)
    controller._spawn_peer_link_client(pairing)
    if fire_rebound:
        _fire_offloader_pair_endpoint_rebound(
            controller,
            pin_sha256=pairing.pin_sha256,
            receiver_hostname=pairing.receiver_hostname,
            receiver_port=pairing.receiver_port,
        )


def maybe_schedule_rebind_probe(controller: OffloaderController, peer: RemoteBuildPeer) -> None:
    """Spawn a probe-and-rebind task if *peer* is a known pin at a new endpoint.

    Called from :func:`.discovery.upsert_host` on every
    resolved broadcast. Cheap early-returns dominate (most
    discoveries are unpaired peers or steady-state
    re-announces); only a rare hostname / port change for an
    APPROVED pairing spawns a probe task. The probe slot is
    rate-limited via :attr:`OffloaderController._rebind_probe_until`
    so a burst of zeroconf Updated callbacks or a
    permanently-unreachable host both collapse to one probe
    per :data:`_REBIND_PROBE_COOLDOWN_SECONDS`.
    """
    pin = peer.pin_sha256
    new_port = peer.remote_build_port
    if not pin or new_port == 0:
        return
    pairing = controller.state.pairings.get(pin)
    if pairing is None or pairing.status is not PeerStatus.APPROVED:
        return
    new_hostname = normalize_hostname(peer.hostname)
    if endpoints_equal(pairing.receiver_hostname, pairing.receiver_port, new_hostname, new_port):
        return
    if controller.state.offloader_peer_link_priv is None:
        return
    now = time.monotonic()
    if controller.state.rebind_probe_until.get(pin, 0.0) > now:
        return
    controller.state.rebind_probe_until[pin] = now + _REBIND_PROBE_COOLDOWN_SECONDS
    controller._track_task(
        controller._probe_and_rebind_endpoint(
            pairing=pairing, new_hostname=new_hostname, new_port=new_port
        ),
        name=f"rebind-probe-{pin[:8]}",
    )


async def probe_and_rebind_endpoint(
    controller: OffloaderController,
    *,
    pairing: StoredPairing,
    new_hostname: str,
    new_port: int,
) -> None:
    """Probe the candidate endpoint; rebind the pairing iff the pin still matches.

    One ``preview`` round-trip checks reachability + identity
    in one call. ``preview`` bypasses the pairing window so a
    quiet receiver doesn't deadlock the rebind path. On a
    successful match, mutate :class:`StoredPairing` in place,
    schedule the debounced save, cancel + respawn the
    peer-link client at the new coordinates, fire
    :attr:`EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND`, and
    clear the cooldown so a future move is probed
    immediately. Failure paths leave the cooldown in place.
    """
    with _clear_cooldown_on_unexpected_exit(controller, pairing.pin_sha256):
        await _probe_log_and_commit(
            controller,
            pairing,
            new_hostname=new_hostname,
            new_port=new_port,
            log_prefix="rebind probe",
        )


async def handle_connect_back(  # noqa: PLR0911 — one reply per refusal / probe outcome
    controller: OffloaderController,
    *,
    pin_sha256: str,
    peer_ip: str,
    announced_port: int,
) -> IntentOutcome:
    """
    Handle a receiver's ``connect_back`` announce: probe, then rebind on match.

    Candidate endpoint is the reverse connection's source IP + the
    announced port; never persisted without a successful forward
    probe. A live forward link or an in-flight probe refuses it.
    """
    pairing = controller.state.pairings.get(pin_sha256)
    if pairing is None or pairing.status is not PeerStatus.APPROVED:
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.NO_APPROVED_PEER)
    if port_or_zero(announced_port) == 0 or not is_ip_address(peer_ip):
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.BAD_ENDPOINT)
    if controller.state.offloader_peer_link_priv is None:
        _LOGGER.warning("connect-back from %s refused: offloader identity not loaded", pin_sha256)
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.PROBE_FAILED)
    if pin_sha256 in controller.state.open_peer_links:
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.ALREADY_CONNECTED)
    now = time.monotonic()
    if controller.state.rebind_probe_until.get(pin_sha256, 0.0) > now:
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.REBIND_IN_PROGRESS)
    controller.state.rebind_probe_until[pin_sha256] = now + _REBIND_PROBE_COOLDOWN_SECONDS
    with _clear_cooldown_on_unexpected_exit(controller, pin_sha256):
        outcome = await _probe_log_and_commit(
            controller,
            pairing,
            new_hostname=peer_ip,
            new_port=announced_port,
            log_prefix="connect-back probe",
        )
    if outcome is RebindProbeOutcome.OK:
        return IntentOutcome(IntentResponse.OK)
    if outcome in (RebindProbeOutcome.UNREACHABLE, RebindProbeOutcome.PIN_MISMATCH):
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.PROBE_FAILED)
    # PAIRING_REPLACED / STATUS_CHANGED — the row moved on
    # mid-probe; the receiver should stop dialing.
    return IntentOutcome(IntentResponse.REJECTED, RejectReason.NO_APPROVED_PEER)


async def _probe_log_and_commit(
    controller: OffloaderController,
    pairing: StoredPairing,
    *,
    new_hostname: str,
    new_port: int,
    log_prefix: str,
) -> RebindProbeOutcome:
    """Probe the candidate endpoint, log the outcome, and commit the rebind on OK."""
    pin = pairing.pin_sha256
    result = await controller._probe_pairing_endpoint(
        pairing=pairing, new_hostname=new_hostname, new_port=new_port
    )
    if result.outcome is RebindProbeOutcome.UNREACHABLE:
        # Pass the captured ``PeerLinkClientError`` as
        # ``exc_info=`` so the debug log carries the
        # full traceback for diagnosing handshake /
        # connect failures in the field — same shape
        # the inline ``except`` block had before this
        # path was factored into ``_probe_pairing_endpoint``.
        _LOGGER.debug(
            "%s %s -> %s:%d failed (unreachable / handshake error)",
            log_prefix,
            pin,
            new_hostname,
            new_port,
            exc_info=result.transport_error,
        )
    elif result.outcome is RebindProbeOutcome.PIN_MISMATCH:
        _LOGGER.warning(
            "%s %s -> %s:%d observed pin %s; ignoring (spoof or rotation)",
            log_prefix,
            pin,
            new_hostname,
            new_port,
            result.observed_pin,
        )
    elif result.outcome is RebindProbeOutcome.OK:
        await controller._commit_endpoint_rebind(pairing, hostname=new_hostname, port=new_port)
        _LOGGER.info("rebound pairing %s to %s:%d", pin, new_hostname, new_port)
    # PAIRING_REPLACED / STATUS_CHANGED — silent skip; cooldown
    # stays in place so a burst of retries doesn't re-fire the
    # probe against state that's already moved on.
    return result.outcome


@contextmanager
def _clear_cooldown_on_unexpected_exit(controller: OffloaderController, pin: str) -> Iterator[None]:
    """Pop *pin* from ``_rebind_probe_until`` iff the wrapped block raises.

    Graceful failure paths inside the probe (unreachable
    host, pin mismatch, mid-probe re-pair) preserve the
    cooldown entry to throttle retries. Cancellation /
    unexpected exceptions shouldn't lock the pin out of
    future legitimate rebind attempts, so on any escaped
    exception we drop the entry before the exception
    propagates.
    """
    try:
        yield
    except BaseException:
        controller.state.rebind_probe_until.pop(pin, None)
        raise


def _fire_offloader_pair_endpoint_rebound(
    controller: OffloaderController,
    *,
    pin_sha256: str,
    receiver_hostname: str,
    receiver_port: int,
) -> None:
    """Fire ``OFFLOADER_PAIR_ENDPOINT_REBOUND`` after a successful rebind."""
    payload: OffloaderPairEndpointReboundData = {
        "pin_sha256": pin_sha256,
        "receiver_hostname": receiver_hostname,
        "receiver_port": receiver_port,
    }
    controller._db.bus.fire(EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND, payload)
