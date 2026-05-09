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

Scope of this part 2 PR:

* :func:`preview_pair` — runs an ``intent="preview"`` round-trip
  and returns the receiver's ``pin_sha256`` for OOB display.
  Used by the new ``remote_build/preview_pair`` WS command.

Subsequent 4a-o PRs add ``request_pair`` (part 3,
:func:`request_pair_handshake`-style helper) and
``intent="pair_status"`` polling (part 4, used by
``list_pool``); the shared driver below is shaped so they can
slot in without re-implementing the connect / read / write
plumbing.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import aiohttp

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

# Total budget for the preview round-trip: TCP connect + WS
# upgrade + 3 Noise messages + post-handshake response + clean
# close. Bounded by LAN latency + the receiver's own per-step
# timeout (10s in
# ``remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS``);
# 10s here matches that budget so we don't give up before the
# receiver does, but doesn't pin a coroutine forever if the
# remote side is gone.
_PREVIEW_TIMEOUT_SECONDS = 10.0


class PeerLinkClientError(RuntimeError):
    """Raised on transport / handshake / decrypt failure on the offloader side.

    Wraps the underlying ``aiohttp.ClientError`` /
    :class:`OSError` / :class:`asyncio.TimeoutError` /
    :data:`NOISE_ERRORS` chain into one type the WS-command
    layer can map to a single ``UNAVAILABLE`` :class:`CommandError`
    without having to enumerate every transport failure mode.
    """


def _build_ws_url(hostname: str, port: int) -> str:
    """Render the peer-link WS URL for *hostname* / *port*.

    ``urllib.parse.quote`` on the hostname so a stray pathological
    character (e.g. an IPv6 literal that should have been bracketed
    by the caller, or a hostname containing a slash) can't smuggle
    a different path or query into the URL. The receiver listens on
    plain TCP — Noise XX provides the transport security — so the
    scheme is ``ws://`` not ``wss://``.
    """
    safe_host = quote(hostname, safe="")
    return f"ws://{safe_host}:{port}{PEER_LINK_PATH}"


async def preview_pair(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
) -> str:
    """Run an ``intent="preview"`` round-trip; return the receiver's pin_sha256.

    Drives the three Noise XX messages from the initiator side:

    1. msg1 — send ``{"intent": "preview"}`` cleartext-but-noise-
       framed (msg1's payload is plaintext on the wire per the
       Noise XX wire spec; coarse intent only, no sensitive
       fields).
    2. msg2 — receive the responder's ephemeral + static; the
       library's read-message is what actually places
       ``static_x25519_pub`` into our handshake state.
    3. msg3 — send our static (empty payload; the receiver
       already knows what it needs after msg2 from the
       offloader-side perspective).

    Then drains the post-handshake transport frame the receiver
    sends (``{"intent_response": "ok"}``) so the WS close is
    clean; the value is asserted (``OK`` is the only non-error
    response on a successful preview) but not surfaced to the
    caller — preview's only output is the captured pin.

    Raises :class:`PeerLinkClientError` on any transport,
    handshake, or decode failure with the underlying exception
    attached as ``__cause__`` for log inspection.
    """
    sess = PeerLinkNoiseSession.initiator(identity_priv)
    url = _build_ws_url(hostname, port)
    timeout = aiohttp.ClientTimeout(total=_PREVIEW_TIMEOUT_SECONDS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.ws_connect(url) as ws,
        ):
            # msg1: plaintext intent
            msg1_payload = _json.dumps({"intent": PeerLinkIntent.PREVIEW.value})
            await ws.send_bytes(sess.write_handshake_message(msg1_payload))
            # msg2: receiver's ephemeral + static. ``receive_bytes``
            # raises if the frame is wrong type; that surfaces as
            # ``aiohttp.ClientError``.
            sess.read_handshake_message(await ws.receive_bytes())
            # msg3: our static; preview carries no msg3 payload.
            await ws.send_bytes(sess.write_handshake_message(b""))
            # Post-handshake transport frame. Drain for clean
            # close; assert OK so a future receiver bug that
            # rejects preview gets surfaced rather than masked.
            response_ct = await ws.receive_bytes()
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        msg = f"peer-link preview to {hostname}:{port} failed: {exc}"
        _LOGGER.debug(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc
    except NOISE_ERRORS as exc:
        msg = f"peer-link preview Noise handshake failed: {exc}"
        _LOGGER.warning(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc

    # ``NOISE_ERRORS`` is a tuple of classes; Python's ``except``
    # syntax doesn't accept nested tuples, so star-unpack it into
    # a flat tuple alongside the JSON decode error.
    try:
        response = _json.loads(sess.decrypt(response_ct))
    except (*NOISE_ERRORS, _json.JSONDecodeError) as exc:
        msg = f"peer-link preview response decode failed: {exc}"
        _LOGGER.warning(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc

    intent_response = response.get("intent_response") if isinstance(response, dict) else None
    if intent_response != IntentResponse.OK.value:
        msg = f"peer-link preview rejected with intent_response={intent_response!r}"
        raise PeerLinkClientError(msg)

    try:
        remote_static = sess.remote_static_pub
    except HandshakeNotCompleteError as exc:
        msg = "peer-link preview handshake completed without capturing remote static pubkey"
        raise PeerLinkClientError(msg) from exc
    return pin_sha256_for_pubkey(remote_static)
