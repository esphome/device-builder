"""
Offloader-side peer-link client value types: errors + results + session state.

The types here describe inputs and outputs of the four
offloader-side intents the peer-link client drives —
``preview`` / ``pair_request`` / ``pair_status`` / ``peer_link``
— plus the per-session in-flight state the long-lived
:class:`PeerLinkClient` keeps alive between handshake and
close.

Public surface (every type without a leading underscore):

* :class:`PeerLinkClientError`,
  :class:`PeerLinkNoSessionError`,
  :class:`SubmitJobTimeoutError`,
  :class:`SubmitJobSessionLostError`,
  :class:`DownloadArtifactsError` — the typed exceptions the
  WS-command layer maps onto the matching
  :class:`CommandError` shape.
* :class:`InitiatorRoundTrip` — output of the shared Noise XX
  driver :func:`drive_initiator_round_trip`.
* :class:`RequestPairResult`, :class:`PairStatusResult`,
  :class:`DownloadArtifactsResult` — outputs of the per-intent
  drivers / :class:`PeerLinkClient` methods.

Private surface (underscore-prefixed) is in-session state
held by :class:`PeerLinkClient` and never crosses the module
boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...models import IntentResponse

if TYPE_CHECKING:
    from ...helpers.peer_link_bundle import BundleAssembler


class PeerLinkClientError(RuntimeError):
    """Raised on transport / handshake / decrypt failure on the offloader side.

    Wraps the underlying ``aiohttp.ClientError`` /
    :class:`OSError` / :class:`asyncio.TimeoutError` /
    :data:`NOISE_ERRORS` chain into one type the WS-command
    layer can map to a single ``UNAVAILABLE`` :class:`CommandError`
    without having to enumerate every transport failure mode.
    """


class PeerLinkPinMismatchError(PeerLinkClientError):
    """
    The responder's static key didn't hash to the pinned pin.

    Raised after msg2, before the msg3 payload (which may carry a
    secret) is written.
    """

    def __init__(self, observed_pin_sha256: str) -> None:
        super().__init__(f"responder pin mismatch (observed {observed_pin_sha256})")
        self.observed_pin_sha256 = observed_pin_sha256


@dataclass(frozen=True)
class PreviewResult:
    """Outcome of an ``intent="preview"`` round-trip.

    ``requires_pairing_key`` is the receiver reporting it's a
    key-mode (``--remote-build-only``) server, so the offloader UI
    requires the bootstrap key up front. Static on the server's mode
    (not on whether a window is open), so it never reveals the
    window-open state.
    """

    pin_sha256: str
    requires_pairing_key: bool


@dataclass(frozen=True)
class InitiatorRoundTrip:
    """One offloader-side Noise XX round-trip's outputs.

    Returned by :func:`drive_initiator_round_trip`. Bundles
    everything a caller might need from a completed handshake +
    response: the receiver's pubkey (so :func:`preview_pair`
    can hash it), the ``intent_response`` value (so callers can
    branch on PENDING / APPROVED / REJECTED / NO_PAIRING_WINDOW),
    and the full decoded response dict for any future fields a
    caller wants beyond the discriminator.
    """

    intent_response: str
    remote_static_pub: bytes
    response: dict[str, Any]


@dataclass(frozen=True)
class RequestPairResult:
    """Outcome of an ``intent="pair_request"`` round-trip from the offloader.

    Returned by :func:`request_pair` after the Noise XX
    handshake completes and the receiver's
    ``intent_response`` has been received.

    * :attr:`status` carries the receiver's response verbatim
      (``IntentResponse.PENDING`` for a freshly-created /
      refreshed pending row, ``IntentResponse.APPROVED`` for a
      re-pair against a pre-existing approved row,
      ``IntentResponse.REJECTED`` for receiver-side decline /
      pin mismatch, ``IntentResponse.NO_PAIRING_WINDOW`` for
      a closed window).
    * :attr:`pin_sha256` is the lowercase-hex hash of the
      receiver's static X25519 pubkey actually observed on the
      live handshake. The caller compares this against the
      pin the user OOB-confirmed in ``preview_pair``; a
      mismatch indicates the receiver rotated identity (or an
      active MITM intervened) between preview and request.
    * :attr:`remote_static_pub` is the raw 32-byte pubkey
      itself, for storage in :class:`StoredPairing`.
    """

    status: IntentResponse
    pin_sha256: str
    remote_static_pub: bytes


@dataclass(frozen=True)
class PairStatusResult:
    """Outcome of an ``intent="pair_status"`` long-poll round-trip.

    Returned by :func:`await_pair_status` after the Noise XX
    handshake completes and the receiver's ``intent_response``
    has been received. The receiver-side handler parks
    indefinitely on the bus event channel until either an admin
    click flips the row or the pairing window closes (firing
    removed events that wake the wait), so the round-trip can
    legitimately take seconds to many minutes; the caller's
    listener task is the only thing that puts an upper bound
    on how long the WS stays open (via cancellation on
    ``unpair`` / controller stop).

    * :attr:`status` is the receiver's verbatim response —
      :attr:`IntentResponse.APPROVED` if the matching
      ``StoredPeer`` row is APPROVED, or
      :attr:`IntentResponse.REJECTED` if no row matches (admin
      clicked Reject, or window-close cleared the receiver's
      pending dict, or pin drift on the receiver side). The
      caller flips local state accordingly.
      :attr:`IntentResponse.PENDING` doesn't appear on this
      path — the receiver doesn't return PENDING from
      ``intent="pair_status"``; the long-poll keeps waiting.
    * :attr:`pin_sha256` is the lowercase-hex hash of the
      receiver's static X25519 pubkey observed on the live
      handshake. The :class:`StoredPairing` consumer compares
      this against its stored ``pin_sha256`` so a receiver-side
      identity rotation between :func:`request_pair` and the
      first :func:`await_pair_status` doesn't silently slide a
      compromised pubkey into ``APPROVED`` state.
    """

    status: IntentResponse
    pin_sha256: str


class PeerLinkNoSessionError(RuntimeError):
    """Raised when a peer-link application send needs a live session and there isn't one.

    Used by every :class:`PeerLinkClient` sender that requires
    the post-handshake dispatch loop to be parked:
    :meth:`PeerLinkClient.submit_job` and
    :meth:`PeerLinkClient.cancel_job`. The check funnels through
    :meth:`PeerLinkClient._require_open_channel`,
    so a future application-message sender that calls
    ``_require_open_channel`` inherits the same exception
    automatically.

    The WS command on the controller side maps this to a typed
    ``CommandError(PRECONDITION_FAILED)`` so the frontend can
    branch on "peer is paired but currently disconnected" vs.
    "send rejected by the receiver." Same error code at every
    call site — the user's recovery (wait for reconnect, retry)
    doesn't depend on which sender raised.
    """


class SubmitJobTimeoutError(RuntimeError):
    """Raised by :meth:`PeerLinkClient.submit_job` when the ack didn't land in time.

    The session may still be alive on the wire — the receiver
    just hasn't acked. Surfaces a structured error to the WS
    caller; the offloader does **not** retry mid-session
    because the receiver may have already accepted and queued
    the job (a duplicate send would land a second
    :class:`FirmwareJob` on the receiver's queue under a fresh
    ``job_id``). Operator-initiated retry on a fresh session
    is the correct recovery.
    """


class SubmitJobSessionLostError(RuntimeError):
    """Raised when the session closes during a :meth:`PeerLinkClient.submit_job` flow.

    Set on every pending ack future from the receive loop's
    ``finally`` so an in-flight :meth:`submit_job` doesn't hang
    until the timeout — the session ended, no ack will ever
    arrive on this connection. Same no-retry contract as
    :class:`SubmitJobTimeoutError`.
    """


class DownloadArtifactsError(RuntimeError):
    """Raised by :meth:`PeerLinkClient.download_artifacts` on failure.

    Carries the structured ``reason`` the receiver included in
    its ``artifacts_end{accepted: false}`` ack
    (``unknown_job`` / ``job_not_completed`` /
    ``build_dir_missing`` / ``pack_failed`` /
    ``duplicate_download``) so the WS layer can surface it to
    the user verbatim. Also raised for offloader-side
    assembly failures: hash mismatch on the final tarball,
    chunk-count drift, post-completion frames.

    Same no-retry contract as :class:`SubmitJobTimeoutError` —
    the receiver may have streamed the bytes already; a
    duplicate request would just refetch them. Operator
    initiates retry on a fresh download.
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DownloadArtifactsResult:
    """Successful return from :meth:`PeerLinkClient.download_artifacts`.

    ``tarball`` is the SHA-256-verified gzipped-tar bytes the
    receiver streamed back; ``firmware_offset`` is the
    receiver-resolved flash-partition offset for
    ``firmware.bin`` (taken verbatim from the
    :attr:`ArtifactsStartFrameData.firmware_offset` header).
    The WS layer's unpack
    (:func:`controllers.remote_build.controller._unpack_artifacts_response`)
    needs both pieces — the tarball doesn't carry the
    firmware partition's offset, only the ``extra``
    flash-image entries do.
    """

    tarball: bytes
    firmware_offset: str


# Per-download state on :attr:`PeerLinkClient._artifacts_downloads`.
# Holds the in-flight :class:`BundleAssembler` (configured
# with :data:`FIRMWARE_MAX_TOTAL_BYTES`) plus the result
# future :meth:`download_artifacts` is parked on. The
# assembler is None until the receiver's ``artifacts_start``
# header lands; the future is created upfront so the
# ``download_artifacts`` flow can register before sending
# the request.
@dataclass
class _DownloadArtifactsState:
    """In-flight artifacts download state (per-``job_id`` on this session)."""

    future: asyncio.Future[DownloadArtifactsResult]
    assembler: BundleAssembler | None = None
    firmware_offset: str = ""


@dataclass
class _SessionLoopState:
    """Mutable state shared between the session's receive loop and heartbeat task.

    Held by :meth:`PeerLinkClient._run_session_loops` and read /
    written by both the receive loop and the heartbeat
    callback so close-cause information flows in either
    direction.

    The receive loop bumps :attr:`last_pong_at` on each pong;
    the heartbeat task reads it through a ``lambda`` to decide
    whether to fire ``on_dead``. The receive loop and the
    heartbeat task each write :attr:`close_reason` on the
    branches they own — receive loop on transport-error /
    terminate-from-peer / unknown-msg-type, heartbeat on
    timeout — so the final close reason reflects the real
    cause rather than falling back to ``peer_hung_up`` (the
    "WS exited iteration without anyone setting a reason"
    default).

    Lifting this out of the receive loop's locals into a small
    object avoids the ``nonlocal`` pattern that would otherwise
    have to be threaded through the heartbeat closure.
    """

    last_pong_at: float
    close_reason: str
