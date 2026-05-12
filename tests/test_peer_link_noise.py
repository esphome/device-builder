"""
Tests for the Noise XX framing helper.

Round-trips the 3-message XX handshake between two
:class:`PeerLinkNoiseSession` instances, asserts the derived
session keys agree (transport-encrypt round-trip), and pins the
remote-static-pubkey capture (the workaround for noiseprotocol's
post-handshake state cleanup).
"""

from __future__ import annotations

import hashlib
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from noise.connection import Keypair, NoiseConnection

from esphome_device_builder.helpers.peer_link_noise import (
    NOISE_PATTERN,
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    _cached_static_keypair,
    pin_sha256_for_pubkey,
)


def _fresh_keypairs() -> tuple[bytes, bytes]:
    return os.urandom(32), os.urandom(32)


def _drive_xx_handshake(
    init: PeerLinkNoiseSession,
    resp: PeerLinkNoiseSession,
    *,
    msg1_payload: bytes = b"",
    msg2_payload: bytes = b"",
    msg3_payload: bytes = b"",
) -> tuple[bytes, bytes, bytes]:
    """Drive the 3 XX messages and return the decrypted payloads in order."""
    m1 = init.write_handshake_message(msg1_payload)
    p1 = resp.read_handshake_message(m1)
    m2 = resp.write_handshake_message(msg2_payload)
    p2 = init.read_handshake_message(m2)
    m3 = init.write_handshake_message(msg3_payload)
    p3 = resp.read_handshake_message(m3)
    return p1, p2, p3


def test_handshake_completes_on_both_sides() -> None:
    init_priv, resp_priv = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    resp = PeerLinkNoiseSession.responder(resp_priv)
    _drive_xx_handshake(init, resp)
    assert init.handshake_finished
    assert resp.handshake_finished


def test_handshake_payloads_round_trip() -> None:
    """Each XX message can carry an application payload; both sides see the same bytes."""
    init_priv, resp_priv = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    resp = PeerLinkNoiseSession.responder(resp_priv)
    p1, p2, p3 = _drive_xx_handshake(
        init,
        resp,
        msg1_payload=b'{"intent":"pair_request"}',
        msg2_payload=b'{"intent_response":"pending"}',
        msg3_payload=b"",
    )
    assert p1 == b'{"intent":"pair_request"}'
    assert p2 == b'{"intent_response":"pending"}'
    assert p3 == b""


def test_each_side_captures_peer_static_pubkey() -> None:
    """
    Each side learns the OTHER side's static pubkey via XX.

    Pins the held-handshake_state-reference workaround that lets
    us read ``rs.public_bytes`` after the handshake completes
    (noiseprotocol clears the protocol's own reference at
    ``Split()``).
    """
    init_priv, resp_priv = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    resp = PeerLinkNoiseSession.responder(resp_priv)
    _drive_xx_handshake(init, resp)

    # Each side knows its own raw private; the corresponding pubkey
    # is what the OTHER side's session should report as remote_static_pub.
    init_pub = X25519PrivateKey.from_private_bytes(init_priv).public_key().public_bytes_raw()
    resp_pub = X25519PrivateKey.from_private_bytes(resp_priv).public_key().public_bytes_raw()

    assert init.remote_static_pub == resp_pub, "initiator should see the responder's static pubkey"
    assert resp.remote_static_pub == init_pub, "responder should see the initiator's static pubkey"


def test_transport_encryption_round_trips() -> None:
    """Post-handshake encrypt → decrypt yields the original bytes; both directions independent."""
    init_priv, resp_priv = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    resp = PeerLinkNoiseSession.responder(resp_priv)
    _drive_xx_handshake(init, resp)

    # Initiator → responder
    ct = init.encrypt(b"submit_job: kitchen.yaml")
    assert ct != b"submit_job: kitchen.yaml"  # ciphertext differs from plaintext
    assert resp.decrypt(ct) == b"submit_job: kitchen.yaml"

    # Responder → initiator (separate cipher state per direction)
    ct = resp.encrypt(b"queue_status: idle")
    assert init.decrypt(ct) == b"queue_status: idle"


def test_remote_static_pub_unavailable_before_msg2_on_initiator() -> None:
    """
    Initiator can't read the remote static until it has read msg2.

    Pins the failure mode so callers don't accidentally peek too
    early (would silently return stale / None data without the
    explicit raise).
    """
    init_priv, _ = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    init.write_handshake_message(b"")  # msg1: no info about responder yet
    with pytest.raises(HandshakeNotCompleteError):
        _ = init.remote_static_pub


def test_encrypt_before_handshake_raises() -> None:
    init_priv, _ = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    with pytest.raises(HandshakeNotCompleteError):
        init.encrypt(b"early!")


def test_decrypt_before_handshake_raises() -> None:
    init_priv, _ = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    with pytest.raises(HandshakeNotCompleteError):
        init.decrypt(b"\x00" * 16)


def test_handshake_hash_available_after_completion() -> None:
    """Handshake hash is identical on both sides after completion (channel-binding)."""
    init_priv, resp_priv = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    resp = PeerLinkNoiseSession.responder(resp_priv)
    _drive_xx_handshake(init, resp)
    assert init.handshake_hash == resp.handshake_hash
    # SHA-256 → 32 bytes
    assert len(init.handshake_hash) == 32


def test_handshake_hash_unavailable_before_completion() -> None:
    init_priv, _ = _fresh_keypairs()
    init = PeerLinkNoiseSession.initiator(init_priv)
    with pytest.raises(HandshakeNotCompleteError):
        _ = init.handshake_hash


def test_pin_sha256_for_pubkey_is_lowercase_hex_sha256() -> None:
    """The on-the-wire pin form is just the lowercase-hex SHA-256 of the raw pubkey bytes."""
    pubkey = os.urandom(32)
    expected = hashlib.sha256(pubkey).hexdigest()
    assert pin_sha256_for_pubkey(pubkey) == expected
    # 64 hex chars
    assert len(pin_sha256_for_pubkey(pubkey)) == 64
    # Lowercase
    assert pin_sha256_for_pubkey(pubkey) == pin_sha256_for_pubkey(pubkey).lower()


def test_distinct_sessions_derive_distinct_keys() -> None:
    """Two independent handshakes (same keypairs) derive different session keys per session."""
    init_priv, resp_priv = _fresh_keypairs()

    sessions = []
    for _ in range(2):
        init = PeerLinkNoiseSession.initiator(init_priv)
        resp = PeerLinkNoiseSession.responder(resp_priv)
        _drive_xx_handshake(init, resp)
        sessions.append((init, resp))

    # Encrypt the same plaintext under both sessions; ciphertexts
    # should differ because the ephemeral keys are fresh per session.
    ct_a = sessions[0][0].encrypt(b"hello")
    ct_b = sessions[1][0].encrypt(b"hello")
    assert ct_a != ct_b

    # And each session's ciphertext only round-trips with its own peer.
    assert sessions[0][1].decrypt(ct_a) == b"hello"
    assert sessions[1][1].decrypt(ct_b) == b"hello"


def test_static_keypair_is_cached_across_sessions() -> None:
    """Two sessions built from the same priv reuse one ``KeyPair25519`` instance."""
    _cached_static_keypair.cache_clear()
    priv = os.urandom(32)
    PeerLinkNoiseSession.initiator(priv)
    PeerLinkNoiseSession.responder(priv)
    info = _cached_static_keypair.cache_info()
    # First call missed (built the keypair); second hit the cache.
    assert info.misses == 1
    assert info.hits == 1


def test_cached_keypair_matches_upstream_derive_path() -> None:
    """Cached path produces a keypair matching ``set_keypair_from_private_bytes``.

    Canary against the noiseprotocol-internal ``keypairs['s']``
    slot we assign into: a rename or KeyPair-shape change
    upstream fires here with a clear pubkey mismatch instead of
    a silent broken session.
    """
    priv = os.urandom(32)
    # Build the slot the upstream-canonical way…
    nc_ref = NoiseConnection.from_name(NOISE_PATTERN)
    nc_ref.set_as_initiator()
    nc_ref.set_keypair_from_private_bytes(Keypair.STATIC, priv)
    ref_kp = nc_ref.noise_protocol.keypairs["s"]

    # …then our cached path; the public bytes must match. If
    # upstream renames the slot or changes its KeyPair shape,
    # one of these assertions fires.
    cached_kp = _cached_static_keypair(priv)
    assert ref_kp is not None
    assert cached_kp.public_bytes == ref_kp.public_bytes
