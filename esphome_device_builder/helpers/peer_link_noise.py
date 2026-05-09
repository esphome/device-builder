"""
Noise XX handshake + ChaCha20-Poly1305 framing for the peer-link WS.

Phase 4a's auth model. Two dashboards meet on the peer-link WS
(``ws://<receiver>:<peer_link_port>/remote-build/peer-link``);
each side holds a long-lived X25519 keypair (its own peer-link
identity, supplied by the caller — keypair lifecycle helper lands
later in 4a-r1). The Noise XX pattern exchanges and authenticates
both static keys, derives a forward-secret session key, and
ChaCha20-Poly1305 wraps every subsequent frame.

Library: ``noiseprotocol`` (already a transitive dep through
``aioesphomeapi``, which uses it with ``Noise_NNpsk0_…`` for the
ESPHome device API; we use ``Noise_XX_25519_ChaChaPoly_SHA256``
because we want mutual identity exchange instead of a pre-shared
key). Reference for the asyncio framing pattern:
``aioesphomeapi/_frame_helper/noise.py``.

This module owns the handshake state machine + the post-handshake
encrypt/decrypt seam. The actual WS plumbing
(``aiohttp.web.WebSocketResponse`` for the receiver,
``aiohttp.ClientSession.ws_connect`` for the offloader) lives in
the controllers that use this helper.

Wire shape for the handshake itself: each Noise XX message is sent
as one binary WS frame. XX produces 3 messages — initiator sends
``e``, responder sends ``e, ee, s, es``, initiator sends ``s, se``.
After the third message both sides have authenticated each other's
static pubkey and derived the same session key.

**Payload confidentiality across the handshake** (load-bearing for
callers that want to put application data in handshake frames):

* **msg1 payload is plaintext on the wire.** A passive sniffer
  observes it verbatim. Don't put sensitive data there. Putting
  a coarse, non-sensitive ``intent`` discriminator there is
  fine if the receiver needs it before completing the handshake.
* **msg2 payload is encrypted** (after the ``es`` token mixes
  the responder's static into the symmetric state). Safe for
  receiver-side application data.
* **msg3 payload is encrypted** (under the now-mixed keys).
  Safe for the initiator's application data — this is where
  sensitive offloader-side fields like ``label`` or
  ``dashboard_id`` belong, not in msg1.

For most use cases the cleanest choice is to leave msg1 / msg2
payloads empty and put all application data in transport frames
after the handshake completes (``encrypt`` / ``decrypt`` below).

Capturing the remote static pubkey: ``noiseprotocol`` wipes the
protocol's ``handshake_state`` reference when the handshake
completes (``Split()`` in ``noise/noise_protocol.py:59``), so we
hold our own reference at construction time and read ``rs.public_bytes``
through it. The held-ref pattern is verified by the unit tests.
"""

from __future__ import annotations

import hashlib
from typing import cast

from noise.connection import Keypair, NoiseConnection
from noise.exceptions import (
    NoiseHandshakeError,
    NoiseInvalidMessage,
    NoiseMaxNonceError,
    NoiseValueError,
)

# Standard Noise pattern name. Same cipher suite the ESPHome device
# API uses (``Noise_NNpsk0_25519_ChaChaPoly_SHA256``); only the
# pattern differs — XX vs NNpsk0 — because we want mutual identity
# exchange, not a pre-shared key.
NOISE_PATTERN = b"Noise_XX_25519_ChaChaPoly_SHA256"

# Tuple-catchable subset of ``noise.exceptions`` for callers that
# want to wrap protocol errors without the verbosity of a 4-element
# ``except`` clause. The library's exceptions don't share a common
# base, so the tuple is the recommended shape per its docs. Used
# by both the responder (``controllers/remote_build_peer_link``) and
# the initiator (``controllers/remote_build_peer_link_client``);
# kept here so a future ``noiseprotocol`` upgrade adding a new
# exception class only has to be threaded through this single
# constant.
NOISE_ERRORS: tuple[type[Exception], ...] = (
    NoiseHandshakeError,
    NoiseInvalidMessage,
    NoiseMaxNonceError,
    NoiseValueError,
)


class HandshakeNotCompleteError(RuntimeError):
    """Raised when accessor needs the handshake to have finished."""


class PeerLinkNoiseSession:
    """
    One side of a peer-link Noise XX session.

    Wraps a :class:`noise.connection.NoiseConnection` and adds:

    * Capture of the remote peer's static X25519 pubkey (needed
      to look up the :class:`StoredPeer` row after handshake;
      the noiseprotocol library would otherwise wipe it when the
      handshake completes).
    * Bytes-out framing: ``encrypt`` / ``decrypt`` return ``bytes``
      rather than ``bytearray`` so the WS layer can send them
      directly.
    * Construction symmetry: ``initiator(...)`` and ``responder(...)``
      classmethods that hide the noiseprotocol setup ceremony
      (set role, set keypair, start handshake).

    Usage shape (offloader side):

        sess = PeerLinkNoiseSession.initiator(our_static_priv)
        ws.send_bytes(sess.write_handshake_message(payload_msg1))
        sess.read_handshake_message(await ws.receive_bytes())  # msg2
        ws.send_bytes(sess.write_handshake_message(b""))        # msg3
        # handshake complete; sess.remote_static_pub now available
        ws.send_bytes(sess.encrypt(b"first transport payload"))

    The receiver side mirrors this with ``responder(...)`` and
    swapped read/write order.
    """

    @classmethod
    def initiator(cls, our_static_priv: bytes) -> PeerLinkNoiseSession:
        """Construct an initiator session bound to *our_static_priv* (32-byte X25519 priv)."""
        nc = NoiseConnection.from_name(NOISE_PATTERN)
        nc.set_as_initiator()
        nc.set_keypair_from_private_bytes(Keypair.STATIC, our_static_priv)
        nc.start_handshake()
        return cls(nc)

    @classmethod
    def responder(cls, our_static_priv: bytes) -> PeerLinkNoiseSession:
        """Construct a responder session bound to *our_static_priv* (32-byte X25519 priv)."""
        nc = NoiseConnection.from_name(NOISE_PATTERN)
        nc.set_as_responder()
        nc.set_keypair_from_private_bytes(Keypair.STATIC, our_static_priv)
        nc.start_handshake()
        return cls(nc)

    def __init__(self, nc: NoiseConnection) -> None:
        self._nc = nc
        # Hold our own reference to handshake_state so we can read
        # ``rs`` (the remote peer's static pubkey) after Noise's
        # ``Split()`` clears the protocol's own reference on
        # handshake completion. See module docstring for the
        # rationale.
        self._hs_ref = nc.noise_protocol.handshake_state
        self._remote_static_pub: bytes | None = None

    # ------------------------------------------------------------------
    # Handshake messages (3 for XX)
    # ------------------------------------------------------------------

    def write_handshake_message(self, payload: bytes = b"") -> bytes:
        """
        Generate the next outbound handshake message.

        *payload* is mixed into the encrypted body of the Noise
        message starting from message 2 onward (XX encrypts the
        payload of msg2 + msg3). Use it to carry application-
        level handshake data like the ``intent`` discriminator,
        the offloader's ``dashboard_id``, ``cert_pem`` for
        pair-requests, etc.
        """
        msg = self._nc.write_message(payload)
        self._capture_remote_static_if_available()
        return bytes(msg)

    def read_handshake_message(self, message: bytes) -> bytes:
        """
        Process an inbound handshake message, return its decrypted payload.

        After the read that completes XX (msg3 from the responder's
        side; never from the initiator's), :attr:`remote_static_pub`
        becomes available.
        """
        payload = self._nc.read_message(message)
        self._capture_remote_static_if_available()
        return bytes(payload)

    def _capture_remote_static_if_available(self) -> None:
        """Stash the peer's static pubkey from the held handshake_state reference."""
        if self._remote_static_pub is not None:
            return
        rs = getattr(self._hs_ref, "rs", None)
        # ``noiseprotocol`` initialises ``rs`` to a placeholder
        # ``Empty`` instance (not ``None``); only when XX has
        # actually carried the peer's static does ``rs`` get
        # replaced with a real KeyPair25519. The duck-typed
        # ``public_bytes`` check distinguishes the two without
        # importing the library's private ``Empty`` class.
        if rs is None or not hasattr(rs, "public_bytes"):
            return
        self._remote_static_pub = bytes(rs.public_bytes)

    # ------------------------------------------------------------------
    # Post-handshake state
    # ------------------------------------------------------------------

    @property
    def handshake_finished(self) -> bool:
        """``True`` once the 3rd handshake message has been processed."""
        # ``noiseprotocol`` ships no type stubs, so
        # ``NoiseConnection.handshake_finished`` arrives as ``Any`` and
        # the raw return trips ``no-any-return``. The runtime contract
        # is documented as ``bool``; cast to pin the signature.
        return cast(bool, self._nc.handshake_finished)

    @property
    def remote_static_pub(self) -> bytes:
        """
        The peer's authenticated static X25519 pubkey (32 bytes).

        Raises :exc:`HandshakeNotCompleteError` if accessed before XX
        has produced the value (i.e. before msg2 read on the
        initiator, or msg3 read on the responder).
        """
        if self._remote_static_pub is None:
            raise HandshakeNotCompleteError(
                "remote static pubkey not available until the relevant XX "
                "handshake message has been read"
            )
        return self._remote_static_pub

    @property
    def handshake_hash(self) -> bytes:
        """
        The Noise handshake hash, a session-unique transcript digest.

        Available only after :attr:`handshake_finished`. Useful for
        channel-binding tokens or for deriving session-scoped keys
        beyond the cipher state (e.g. for key-confirmation
        challenges in higher protocol layers).
        """
        if not self.handshake_finished:
            raise HandshakeNotCompleteError(
                "handshake_hash not available until handshake completes"
            )
        return bytes(self._nc.noise_protocol.handshake_hash)

    # ------------------------------------------------------------------
    # Transport encryption
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: bytes) -> bytes:
        """Wrap *plaintext* in a ChaCha20-Poly1305 transport frame."""
        if not self.handshake_finished:
            raise HandshakeNotCompleteError("encrypt called before handshake completed")
        return bytes(self._nc.encrypt(plaintext))

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Unwrap a ChaCha20-Poly1305 transport frame and return its plaintext."""
        if not self.handshake_finished:
            raise HandshakeNotCompleteError("decrypt called before handshake completed")
        return bytes(self._nc.decrypt(ciphertext))


def pin_sha256_for_pubkey(static_x25519_pub: bytes) -> str:
    """
    Lowercase-hex SHA-256 of a raw 32-byte X25519 pubkey.

    Same wire-friendly form ``StoredPeer.pin_sha256`` /
    ``DashboardIdentity.pin_sha256`` use elsewhere — the OOB-verify
    UI and event payloads work in this representation.
    """
    return hashlib.sha256(static_x25519_pub).hexdigest()
