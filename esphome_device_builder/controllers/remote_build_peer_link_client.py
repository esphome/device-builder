"""
Offloader-side peer-link Noise WS client (issue #106 phase 4a-o part 2).

Initiator counterpart of
:mod:`controllers.remote_build_peer_link`'s responder. Opens a
``ws://<receiver>:<peer_link_port>/remote-build/peer-link``
WebSocket, drives the three Noise XX handshake messages from the
offloader side, optionally exchanges application-level
``intent`` / ``intent_response`` framing, and surfaces the
captured receiver static pubkey hash to the caller.

This module is the wire-shape twin of
``remote_build_peer_link.py``: same handshake, opposite role.
The two share the cipher suite + frame layout via
:mod:`helpers.peer_link_noise` (single :class:`PeerLinkNoiseSession`
class, ``initiator`` / ``responder`` factories) and the same
exception-tuple (:data:`helpers.peer_link_noise.NOISE_ERRORS`)
so a future ``noiseprotocol`` upgrade only has to thread through
one place.

The wire-flow shape — TCP connect, 3 Noise XX messages, post-
handshake transport frame, error mapping — is identical across
every initiator-side intent the offloader needs (``preview``,
``pair_request``, ``pair_status``, eventually ``peer_link``);
only the msg3 payload and which response codes count as success
differ. :func:`drive_initiator_round_trip` owns the shared flow;
each public ``preview_pair`` / ``request_pair`` / ``poll_pair_status``
function (parts 2-4 of phase 4a-o) is a thin wrapper that
provides the intent + msg3 payload + accepted-response set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from yarl import URL

from ..helpers import json as _json
from ..helpers.peer_link_noise import (
    NOISE_ERRORS,
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ..models import IntentResponse, PeerLinkIntent
from .remote_build_peer_link import PEER_LINK_PATH

_LOGGER = logging.getLogger(__name__)

# Flat tuple of every exception class that can land out of the
# decrypt + JSON-parse step. Built once at module level rather
# than inlined as ``(*NOISE_ERRORS, _json.JSONDecodeError)`` in
# the ``except`` clause so mypy can verify the type without
# tripping on its star-unpack-in-except limitation (the runtime
# would handle the inline form fine on Python 3.12+, but the
# type checker can't follow it).
_RESPONSE_DECODE_ERRORS: tuple[type[Exception], ...] = (
    *NOISE_ERRORS,
    _json.JSONDecodeError,
)


# Total budget for one initiator round-trip: TCP connect + WS
# upgrade + 3 Noise messages + post-handshake response + clean
# close. Bounded by LAN latency + the receiver's own per-step
# timeout (10s in
# ``remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS``);
# 10s here matches that budget so we don't give up before the
# receiver does, but doesn't pin a coroutine forever if the
# remote side is gone.
_DEFAULT_TIMEOUT_SECONDS = 10.0


class PeerLinkClientError(RuntimeError):
    """Raised on transport / handshake / decrypt failure on the offloader side.

    Wraps the underlying ``aiohttp.ClientError`` /
    :class:`OSError` / :class:`asyncio.TimeoutError` /
    :data:`NOISE_ERRORS` chain into one type the WS-command
    layer can map to a single ``UNAVAILABLE`` :class:`CommandError`
    without having to enumerate every transport failure mode.
    """


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


def _build_ws_url(hostname: str, port: int) -> URL:
    """Build the peer-link WS URL for *hostname* / *port*.

    Uses :class:`yarl.URL` (already in our dep closure via aiohttp)
    rather than hand-rolled f-string + ``urllib.parse.quote``:

    * IPv6 literals get auto-bracketed (``::1`` →
      ``ws://[::1]:6055/...``); the f-string version would have
      produced an unparsable URL.
    * Pathological characters in the hostname (slash, query
      terminators, fragment markers, embedded ``:port``) raise
      ``ValueError`` loudly instead of getting silently
      percent-encoded into a non-resolvable form. The
      WS-command boundary's ``_validate_hostname`` already
      defers to :class:`yarl.URL.build` for the URL-correctness
      check and rejects these shapes as ``INVALID_ARGS``, so
      the validator and ``_build_ws_url`` share a single source
      of truth on what a host is. A future caller that bypasses
      ``_validate_hostname`` would still get the ``ValueError``
      here; :func:`drive_initiator_round_trip` keeps a
      defense-in-depth catch that maps it to
      :class:`PeerLinkClientError` (→ UNAVAILABLE) so the
      surface contract holds even on the bypass path.
    * Path is given to yarl as a constant; encoding stays
      intact across versions.

    The receiver listens on plain TCP — Noise XX provides the
    transport security — so the scheme is ``ws://`` not
    ``wss://``. Returns a :class:`URL` because
    :meth:`aiohttp.ClientSession.ws_connect` accepts both
    strings and ``URL`` instances; passing the typed shape
    skips one re-parse on the aiohttp side.
    """
    return URL.build(scheme="ws", host=hostname, port=port, path=PEER_LINK_PATH)


async def drive_initiator_round_trip(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
    intent: PeerLinkIntent,
    msg3_payload: bytes = b"",
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> InitiatorRoundTrip:
    """Run one Noise XX round-trip from the initiator side.

    The flow is identical for every offloader-side intent
    (``preview`` / ``pair_request`` / ``pair_status`` /
    ``peer_link``); callers vary only the *intent* discriminator
    (cleartext on msg1) and the encrypted *msg3_payload* (carries
    ``label`` + ``dashboard_id`` for ``pair_request`` and
    similar). The shared driver here keeps the connect / send /
    receive / decode / error-map plumbing in one place so future
    intents don't reinvent it.

    Wire shape, mirroring the receiver-side responder in
    :func:`controllers.remote_build_peer_link._drive_peer_link_session`:

    * msg1 — send ``{"intent": "..."}`` cleartext-but-noise-framed
      (msg1's payload is plaintext on the wire per Noise XX;
      coarse intent only, no sensitive fields).
    * msg2 — receive the responder's ephemeral + static; the
      library's read-message places ``static_x25519_pub`` into
      our handshake state.
    * msg3 — send our static + the *msg3_payload* (encrypted
      under the now-mixed cipher).
    * Post-handshake — receive one transport frame carrying
      ``{"intent_response": "..."}``; decrypt + JSON-parse.

    Raises :class:`PeerLinkClientError` on any transport,
    handshake, or decode failure with the underlying exception
    attached as ``__cause__`` for log inspection. The caller is
    responsible for branching on
    :attr:`InitiatorRoundTrip.intent_response` (each intent has
    its own accept-set).
    """
    sess = PeerLinkNoiseSession.initiator(identity_priv)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    label = f"peer-link {intent.value} to {hostname}:{port}"

    # ``_build_ws_url`` is inside the try block as defense-in-depth.
    # The WS-command boundary's ``_validate_hostname`` defers to
    # :class:`yarl.URL.build` and already rejects
    # path-injection-shaped hosts (slash, ``?``, ``#``, ``@``,
    # embedded ``:port``) as ``INVALID_ARGS``, so on the
    # validator-gated path :meth:`URL.build` here will never
    # raise. But a future caller that calls this driver
    # directly without going through the validator (e.g. a
    # 4a-o part 3/4 helper that takes a stored ``hostname``
    # off disk and assumes it's clean) would otherwise see the
    # ``ValueError`` escape this function and surface as
    # ``INTERNAL_ERROR`` instead of the documented
    # ``UNAVAILABLE`` mapping. Wrapping the build inside the
    # try keeps the contract holding regardless of which entry
    # point the caller used.
    try:
        url = _build_ws_url(hostname, port)
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.ws_connect(url) as ws,
        ):
            msg1 = _json.dumps({"intent": intent.value})
            await ws.send_bytes(sess.write_handshake_message(msg1))
            sess.read_handshake_message(await ws.receive_bytes())
            await ws.send_bytes(sess.write_handshake_message(msg3_payload))
            response_ct = await ws.receive_bytes()
    except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
        msg = f"{label} failed: {exc}"
        _LOGGER.debug(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc
    except NOISE_ERRORS as exc:
        msg = f"{label} Noise handshake failed: {exc}"
        _LOGGER.warning(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc

    try:
        decoded = _json.loads(sess.decrypt(response_ct))
    except _RESPONSE_DECODE_ERRORS as exc:
        msg = f"{label} response decode failed: {exc}"
        _LOGGER.warning(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc

    if not isinstance(decoded, dict):
        msg = f"{label} response was not a JSON object: {decoded!r}"
        raise PeerLinkClientError(msg)
    intent_response = decoded.get("intent_response")
    if not isinstance(intent_response, str):
        msg = f"{label} response missing 'intent_response' string: {decoded!r}"
        raise PeerLinkClientError(msg)

    try:
        remote_static = sess.remote_static_pub
    except HandshakeNotCompleteError as exc:
        msg = f"{label} handshake completed without capturing remote static pubkey"
        raise PeerLinkClientError(msg) from exc

    return InitiatorRoundTrip(
        intent_response=intent_response,
        remote_static_pub=remote_static,
        response=decoded,
    )


async def preview_pair(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
) -> str:
    """Run an ``intent="preview"`` round-trip; return the receiver's pin_sha256.

    Thin wrapper around :func:`drive_initiator_round_trip`:
    preview's accept-set is just ``IntentResponse.OK`` (anything
    else is a receiver-side bug or a misconfigured deployment);
    its msg3 payload is empty (the receiver already has what it
    needs from msg2 from the offloader's perspective).

    The frontend renders the returned ``pin_sha256`` for the
    user to OOB-verify against the receiver's "Build server"
    Settings card; only after that confirmation does the
    offloader call ``request_pair`` (phase 4a-o part 3).
    """
    rt = await drive_initiator_round_trip(
        hostname=hostname,
        port=port,
        identity_priv=identity_priv,
        intent=PeerLinkIntent.PREVIEW,
    )
    if rt.intent_response != IntentResponse.OK.value:
        msg = f"peer-link preview rejected with intent_response={rt.intent_response!r}"
        raise PeerLinkClientError(msg)
    return pin_sha256_for_pubkey(rt.remote_static_pub)
