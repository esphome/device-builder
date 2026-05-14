"""
Persistent peer-link identity (X25519 keypair) for the remote-build feature.

Generates and persists, on first call to
:meth:`PeerLinkIdentityStore.async_load`:

* a 32-byte X25519 private key at
  ``<config_dir>/.device-builder-peer-link-key.bin`` (mode ``0600``)

Subsequent calls return the cached identity. The matching public
key is derived from the private key via :mod:`cryptography`'s
``X25519PrivateKey.public_key().public_bytes_raw()``. The public
half is recomputed each load rather than persisted, so a corrupted
public-key file can't desync from the private half.

This X25519 keypair is the dashboard's sole cryptographic
identity for the remote-build feature. It drives the Noise XX
mutual-authentication handshake in
:class:`~esphome_device_builder.helpers.peer_link_noise.PeerLinkNoiseSession`
and the SHA-256 of its public key is the ``pin_sha256`` that
peers OOB-verify during pairing and broadcast in mDNS TXT.
:mod:`helpers.dashboard_identity` composes this key's
fingerprint with the persistent ``dashboard_id`` for the
Settings UI.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .atomic_io import atomic_write

_LOGGER = logging.getLogger(__name__)

_KEY_FILENAME = ".device-builder-peer-link-key.bin"
_KEY_MODE = 0o600
_KEY_LENGTH = 32  # X25519 private keys are 32 raw bytes


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


class PeerLinkIdentityStore:
    """
    Process-wide async cache for one dashboard's peer-link X25519 identity.

    The :class:`asyncio.Lock` is held across the cache check
    and the executor hop, so loads racing rotations either
    see the pre-rotate identity or wait and see the
    post-rotate one.
    """

    def __init__(self, config_dir: Path) -> None:
        self._key_path = config_dir / _KEY_FILENAME
        self._lock = asyncio.Lock()
        self._cached: PeerLinkIdentity | None = None

    async def async_load(self) -> PeerLinkIdentity:
        """Return the cached identity, generating + persisting on first call."""
        return await asyncio.shield(self._do_load_locked())

    async def async_rotate(self) -> PeerLinkIdentity:
        """
        Replace the on-disk keypair with a fresh X25519 secret.

        Shielded so a cancelled awaiter can't release the lock
        while the unstoppable executor write lands the new key
        on disk; the locked rotation runs to completion in the
        background, keeping cache + disk in sync.
        """
        return await asyncio.shield(self._do_rotate_locked())

    async def _do_load_locked(self) -> PeerLinkIdentity:
        async with self._lock:
            if self._cached is not None:
                return self._cached
            identity = await asyncio.get_running_loop().run_in_executor(None, self._load_blocking)
            self._cached = identity
            return identity

    async def _do_rotate_locked(self) -> PeerLinkIdentity:
        async with self._lock:
            identity = await asyncio.get_running_loop().run_in_executor(None, self._rotate_blocking)
            self._cached = identity
            return identity

    def _load_blocking(self) -> PeerLinkIdentity:
        """Disk read + X25519 derive; runs in the default executor."""
        private_bytes = _load_key(self._key_path)
        if private_bytes is None:
            private_bytes = _generate_key()
            atomic_write(self._key_path, private_bytes, mode=_KEY_MODE)
            _LOGGER.info("Generated new peer-link identity at %s", self._key_path)
        identity = _build_identity(private_bytes)
        _log_loaded_identity(self._key_path, identity.public_bytes, identity.pin_sha256)
        return identity

    def _rotate_blocking(self) -> PeerLinkIdentity:
        """Generate + atomic write + derive; runs in the default executor."""
        private_bytes = _generate_key()
        atomic_write(self._key_path, private_bytes, mode=_KEY_MODE)
        _LOGGER.info("Rotated peer-link identity at %s", self._key_path)
        identity = _build_identity(private_bytes)
        _log_loaded_identity(self._key_path, identity.public_bytes, identity.pin_sha256)
        return identity


def _build_identity(private_bytes: bytes) -> PeerLinkIdentity:
    """Derive the pubkey + pin_sha256 from *private_bytes*."""
    public_bytes = (
        X25519PrivateKey.from_private_bytes(private_bytes).public_key().public_bytes_raw()
    )
    pin_sha256 = hashlib.sha256(public_bytes).hexdigest()
    return PeerLinkIdentity(
        private_bytes=private_bytes,
        public_bytes=public_bytes,
        pin_sha256=pin_sha256,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _log_loaded_identity(key_path: Path, public_bytes: bytes, pin_sha256: str) -> None:
    """Emit one INFO line with key-file stat, raw pubkey hex, and pin."""
    try:
        stat = key_path.stat()
    except OSError as exc:
        _LOGGER.info(
            "Loaded peer-link identity from %s (stat_failed: %s: %s pub=%s pin=%s)",
            key_path,
            type(exc).__name__,
            exc,
            public_bytes.hex(),
            pin_sha256,
        )
        return
    _LOGGER.info(
        "Loaded peer-link identity from %s (size=%d mtime=%.0f pub=%s pin=%s)",
        key_path,
        stat.st_size,
        stat.st_mtime,
        public_bytes.hex(),
        pin_sha256,
    )


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
