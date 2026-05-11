"""
Persistent peer-link identity (X25519 keypair) for the remote-build feature.

Generates and persists, on first call to
:func:`get_or_create_peer_link_identity`:

* a 32-byte X25519 private key at
  ``<config_dir>/.device-builder-peer-link-key.bin`` (mode ``0600``)

Subsequent calls reload the same bytes. The matching public key
is derived from the private key via :mod:`cryptography`'s
``X25519PrivateKey.public_key().public_bytes_raw()``. The public
half is recomputed each load rather than persisted, so a corrupted
public-key file can't desync from the private half.

This identity is **separate** from the dashboard's Ed25519 cert
keypair (``.device-builder-key.pem``). The cert remains the
dashboard's TLS identity for any externally-fronted HTTPS use;
the X25519 keypair here is the dashboard's identity for
:class:`~esphome_device_builder.helpers.peer_link_noise.PeerLinkNoiseSession`
(the Noise XX peer-link channel). The two have independent
rotation lifecycles.

Generation is one ``X25519PrivateKey.generate()`` call plus a
single atomic file write. Sync and blocking; async callers must
hop through ``run_in_executor``.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .atomic_io import atomic_write

_LOGGER = logging.getLogger(__name__)

_KEY_FILENAME = ".device-builder-peer-link-key.bin"
_KEY_MODE = 0o600
_KEY_LENGTH = 32  # X25519 private keys are 32 raw bytes

# Serialise first-time creation so two callers racing don't both
# generate-and-persist a fresh keypair (the loser's atomic write
# would silently invalidate every peer that had already paired
# under the winner's key). Production calls this once at startup;
# the lock handles test-style races + future contention safely.
_IDENTITY_LOCK = threading.Lock()


@dataclass(frozen=True)
class PeerLinkIdentity:
    """
    The persistent peer-link identity for one dashboard installation.

    ``private_bytes`` is the raw 32-byte X25519 secret used by
    :class:`~esphome_device_builder.helpers.peer_link_noise.PeerLinkNoiseSession`
    (passed to :meth:`noise.connection.NoiseConnection.set_keypair_from_private_bytes`).
    ``public_bytes`` is the matching 32-byte X25519 pubkey.
    ``pin_sha256`` is the lowercase-hex SHA-256 of ``public_bytes``;
    the wire-friendly form UIs render for OOB fingerprint
    comparison, and the value mDNS TXT advertises so offloaders
    can pin against it before a Noise handshake.
    """

    private_bytes: bytes
    public_bytes: bytes
    pin_sha256: str

    @property
    def pin_sha256_formatted(self) -> str:
        """Return the pin as space-separated byte pairs for OOB-display."""
        return " ".join(self.pin_sha256[i : i + 2] for i in range(0, len(self.pin_sha256), 2))


def get_or_create_peer_link_identity(config_dir: Path) -> PeerLinkIdentity:
    """
    Load the persistent peer-link identity, generating it on first call.

    Idempotent. An unreadable or wrong-length key file is treated
    as "missing" and regenerated; the previous identity's paired
    peers then see ``pin_mismatch`` events on their next handshake
    and have to re-pair, which is the right user-visible outcome
    when on-disk identity has gone wrong. Length-correct bytes are
    always usable: any 32-byte string is a valid X25519 private
    key after the curve's clamping, so no parse-validation step
    is needed.
    """
    key_path = config_dir / _KEY_FILENAME

    with _IDENTITY_LOCK:
        private_bytes = _load_key(key_path)
        if private_bytes is None:
            private_bytes = _generate_key()
            atomic_write(key_path, private_bytes, mode=_KEY_MODE)
            _LOGGER.info("Generated new peer-link identity at %s", key_path)

    public_bytes = (
        X25519PrivateKey.from_private_bytes(private_bytes).public_key().public_bytes_raw()
    )
    return PeerLinkIdentity(
        private_bytes=private_bytes,
        public_bytes=public_bytes,
        pin_sha256=hashlib.sha256(public_bytes).hexdigest(),
    )


def rotate_peer_link_identity(config_dir: Path) -> PeerLinkIdentity:
    """
    Generate a fresh X25519 keypair, replacing whatever's on disk.

    Forces every receiver that paired with us (when we run as
    offloader) and every offloader paired with us (when we run as
    receiver) to re-pair: their stored ``pin_sha256`` for our
    dashboard no longer matches the pubkey we present in the next
    Noise handshake, so the receiver-side / offloader-side
    ``pin_mismatch`` event fires and the UI prompts re-pair.

    Sync and blocking; async callers must hop through
    ``run_in_executor``.
    """
    key_path = config_dir / _KEY_FILENAME
    with _IDENTITY_LOCK:
        private_bytes = _generate_key()
        atomic_write(key_path, private_bytes, mode=_KEY_MODE)
        _LOGGER.info("Rotated peer-link identity at %s", key_path)

    public_bytes = (
        X25519PrivateKey.from_private_bytes(private_bytes).public_key().public_bytes_raw()
    )
    return PeerLinkIdentity(
        private_bytes=private_bytes,
        public_bytes=public_bytes,
        pin_sha256=hashlib.sha256(public_bytes).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_key(key_path: Path) -> bytes | None:
    """
    Read the persisted X25519 private key, returning ``None`` on any miss.

    Treats wrong-length input as "missing" so the caller regenerates
    rather than failing. A half-written or truncated key file means
    the on-disk state is wrong; the user-visible cost of regenerating
    is "every peer has to re-pair once", the same outcome as a
    deliberate rotation. Any 32-byte string is a valid X25519 private
    key after clamping, so a length-correct read is always usable.
    """
    if not key_path.is_file():
        return None
    try:
        data = key_path.read_bytes()
    except OSError as exc:
        _LOGGER.warning("Could not read peer-link key at %s: %s", key_path, exc)
        return None
    if len(data) != _KEY_LENGTH:
        _LOGGER.warning(
            "Peer-link key at %s has wrong length (%d, expected %d); regenerating",
            key_path,
            len(data),
            _KEY_LENGTH,
        )
        return None
    return data


def _generate_key() -> bytes:
    """Return a fresh raw 32-byte X25519 private key."""
    return X25519PrivateKey.generate().private_bytes_raw()
