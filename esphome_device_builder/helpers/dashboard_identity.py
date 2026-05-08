"""
Persistent dashboard identity for the remote-build feature.

Phase 3a of issue #106. Generates and persists three pieces of
identity that the rest of phase 3+ will consume:

* A long-lived self-signed TLS certificate. Phase 3b serves
  ``/remote-build/v1/*`` over HTTPS using this cert. Stored as a
  PEM file at ``<config_dir>/.device-builder-cert.pem``.
* The matching private key. Stored at
  ``<config_dir>/.device-builder-key.pem`` with mode ``0600`` so a
  metadata-only backup of the config dir doesn't leak it via
  default-permissive copies.
* A stable random ``dashboard_id`` (base64url, 24 bytes of
  entropy). Phase 4 first-use binding pins each issued token to
  the requesting offloader's ``dashboard_id`` so a leaked token
  used from a different installation surfaces a warning.

Everything is generated on first call to
:func:`get_or_create_identity` and reloaded verbatim on every
subsequent call. The cert is valid for 100 years so "expired"
isn't a class of failure we have to handle; rotation is explicit
(future "Rotate certificate" button surfaces in phase 3c).

The :func:`get_or_create_identity` call hits the disk and
generates ~2k of RSA on first run; phase 3b's
:class:`DeviceBuilder.start` runs it once via
:func:`asyncio.AbstractEventLoop.run_in_executor` so the loop
doesn't stall on the initial generation.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ..controllers.config import metadata_transaction

_LOGGER = logging.getLogger(__name__)

_CERT_FILENAME = ".device-builder-cert.pem"
_KEY_FILENAME = ".device-builder-key.pem"
_KEY_MODE = 0o600
_DASHBOARD_ID_BYTES = 24
_CERT_KEY_BITS = 2048
# 100 years; cert rotation is explicit, not driven by expiry.
# Avoids the "user opens dashboard after a long quiet period and
# every paired peer's pinning fails because the cert silently
# expired" footgun.
_CERT_VALIDITY_DAYS = 365 * 100
_CERT_COMMON_NAME = "ESPHome Device Builder"
_REMOTE_BUILD_KEY = "_remote_build"
_DASHBOARD_ID_KEY = "dashboard_id"


@dataclass(frozen=True)
class DashboardIdentity:
    """The persistent identity for one dashboard installation."""

    dashboard_id: str
    cert_pem: bytes
    key_pem: bytes
    cert_sha256: str  # lowercase hex, no separators

    @property
    def cert_sha256_formatted(self) -> str:
        """
        Return the SHA-256 fingerprint as ``aa bb cc ...`` for display.

        Two-character groups separated by spaces, matching the way
        most TLS / certificate UIs render fingerprints. The wire
        form (mDNS TXT, JSON responses) uses the bare hex string in
        :attr:`cert_sha256`.
        """
        return " ".join(self.cert_sha256[i : i + 2] for i in range(0, len(self.cert_sha256), 2))


def get_or_create_identity(config_dir: Path) -> DashboardIdentity:
    """
    Load the persistent identity, generating it on first call.

    Idempotent: every call after the first reads the same files and
    returns the same dashboard_id. The cert + key files are stored
    next to ``.device-builder.json`` in the config directory (never
    in the build tree, never in ``.esphome/``); the
    :data:`_KEY_FILENAME` is written with mode ``0600`` so a
    config-dir backup that includes default-permissive copies
    doesn't accidentally leak it.

    A partially-corrupt state (cert exists but key is missing, or
    one of them is unparsable) is treated as "missing entirely"
    and regenerated. The user-visible cost is "every paired peer
    has to re-pair", which is exactly what you'd want when
    something on disk has gone wrong; the alternative of
    half-trusting a damaged identity is worse.
    """
    cert_path = config_dir / _CERT_FILENAME
    key_path = config_dir / _KEY_FILENAME

    cert_pem, key_pem = _load_cert_pair(cert_path, key_path)
    if cert_pem is None or key_pem is None:
        cert_pem, key_pem = _generate_cert_pair()
        _persist_cert_pair(cert_path, key_path, cert_pem, key_pem)

    dashboard_id = _get_or_create_dashboard_id(config_dir)

    return DashboardIdentity(
        dashboard_id=dashboard_id,
        cert_pem=cert_pem,
        key_pem=key_pem,
        cert_sha256=_cert_fingerprint(cert_pem),
    )


def rotate_certificate(config_dir: Path) -> DashboardIdentity:
    """
    Generate a fresh cert + key, replacing whatever's on disk.

    Keeps the existing ``dashboard_id`` (stable identity across
    rotations; only the cert changes). The "Rotate certificate"
    button in phase 3c's Settings UI is the user-facing trigger;
    every paired peer will see a fingerprint mismatch on the next
    connection and need to re-pair via the wizard from the
    "Re-authentication" section of the issue.
    """
    cert_path = config_dir / _CERT_FILENAME
    key_path = config_dir / _KEY_FILENAME
    cert_pem, key_pem = _generate_cert_pair()
    _persist_cert_pair(cert_path, key_path, cert_pem, key_pem)

    dashboard_id = _get_or_create_dashboard_id(config_dir)

    return DashboardIdentity(
        dashboard_id=dashboard_id,
        cert_pem=cert_pem,
        key_pem=key_pem,
        cert_sha256=_cert_fingerprint(cert_pem),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_cert_pair(cert_path: Path, key_path: Path) -> tuple[bytes | None, bytes | None]:
    """
    Read the persisted cert + key, returning ``(None, None)`` on any miss.

    A partial state (cert without key, key without cert) counts as
    a miss; both files have to be present, parse cleanly, AND match
    each other (the cert's public key must equal the private key's
    public key) or the caller regenerates. The cross-check catches
    a state where someone has manually rotated one half of the pair
    or where a backup-restore reassembled mismatched files; serving
    a mismatched pair as the dashboard identity would fail at TLS
    handshake time with an opaque "key values mismatch" error.

    Also tightens the key file's mode to ``0600`` if a previous
    write left it looser, defending against the case where an older
    version of this helper, an external script, or a botched backup
    restored the file at the umask default.
    """
    if not cert_path.exists() or not key_path.exists():
        return None, None
    try:
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        private_key = serialization.load_pem_private_key(key_pem, password=None)
        # Cross-check: a parsing pass alone passes a stale key
        # paired with a fresh cert (or vice versa). Compare via
        # ``public_numbers()``; the dataclass ``__eq__`` covers
        # both RSA and EC keys.
        if cert.public_key().public_numbers() != private_key.public_key().public_numbers():
            _LOGGER.warning(
                "Persisted cert at %s does not match private key at %s; regenerating",
                cert_path,
                key_path,
            )
            return None, None
    except Exception:
        _LOGGER.debug(
            "Persisted cert / key at %s / %s failed to parse; regenerating",
            cert_path,
            key_path,
            exc_info=True,
        )
        return None, None
    # ``os.open(..., mode=0o600)`` only applies on creation; an
    # existing key file with looser perms keeps them unless we
    # chmod here. Idempotent and cheap.
    with contextlib.suppress(OSError):
        os.chmod(key_path, _KEY_MODE)
    return cert_pem, key_pem


def _generate_cert_pair() -> tuple[bytes, bytes]:
    """Generate a fresh RSA-2048 keypair and a self-signed cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=_CERT_KEY_BITS)
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _CERT_COMMON_NAME)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=_CERT_VALIDITY_DAYS))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """
    Write *data* to *path* atomically with optional creation mode.

    Stages the bytes in a sibling tempfile (``tempfile.mkstemp``
    creates with mode ``0600`` by default), fsync-flushes to disk,
    then ``os.replace``s into place. A reader that opens *path*
    during the write either sees the old bytes or the new bytes,
    never a partial / truncated PEM. ``mode`` is re-applied to the
    final path so the destination ends up at the requested mode
    even if it pre-existed at a looser one. Same atomic-write
    shape as ``controllers.config._save_metadata``.

    On a crash mid-write the tempfile is left behind; the cleanup
    branch unlinks it. The ``os.replace`` itself is atomic on the
    same filesystem (POSIX ``rename`` semantics) so no half-state
    can land at the destination path.
    """
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, path)
    except Exception:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    if mode is not None:
        # Belt-and-braces: ``os.replace`` preserves the source
        # tempfile's mode, but if the destination existed and the
        # umask between mkstemp and replace was loosened by another
        # caller, re-apply now to be sure.
        with contextlib.suppress(OSError):
            os.chmod(path, mode)


def _persist_cert_pair(cert_path: Path, key_path: Path, cert_pem: bytes, key_pem: bytes) -> None:
    """
    Write cert + key atomically, with the key at ``0600``.

    Both files go through :func:`atomic_write` so a reader (or a
    crash) mid-write never observes a truncated PEM. The key write
    runs first; if it succeeds and the cert write fails, the next
    ``get_or_create_identity`` call sees a key-without-matching-cert
    state and regenerates the whole pair (the cross-check in
    :func:`_load_cert_pair` rejects mismatched halves too, so a
    leftover stale cert can't pair with a fresh key by accident).

    Cert is public-by-design but ends up at ``0600`` too because
    ``tempfile.mkstemp`` defaults to that; tightening the cert
    isn't a problem (only this dashboard's process needs to read
    it for HTTPS) and skipping the explicit chmod simplifies the
    code.
    """
    atomic_write(key_path, key_pem, mode=_KEY_MODE)
    atomic_write(cert_path, cert_pem)


def _cert_fingerprint(cert_pem: bytes) -> str:
    """SHA-256 fingerprint of the DER-encoded cert as lowercase hex."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def _generate_dashboard_id() -> str:
    """Return a random base64url string identifying this dashboard installation."""
    return secrets.token_urlsafe(_DASHBOARD_ID_BYTES)


def _get_or_create_dashboard_id(config_dir: Path) -> str:
    """
    Return the persistent ``dashboard_id``, generating one if absent.

    Routes through :func:`controllers.config.metadata_transaction`
    so the read-modify-write happens under the same lock that
    serialises every other writer of ``.device-builder.json``.
    Two concurrent callers (one from dashboard startup, one from
    ``rotate_certificate`` firing on the user's button click)
    can't both observe an empty ``dashboard_id`` and have one of
    them silently overwrite the other; the lock makes the
    "exists?" check and the "fresh generate + persist" step
    atomic against any other ``_remote_build`` mutation. The
    transaction also handles atomic-save on the metadata file
    via the same ``tempfile + os.replace`` pattern this helper
    uses for the cert / key files.
    """
    with metadata_transaction(config_dir) as data:
        rb = data.get(_REMOTE_BUILD_KEY)
        if not isinstance(rb, dict):
            rb = {}
            data[_REMOTE_BUILD_KEY] = rb
        existing = rb.get(_DASHBOARD_ID_KEY)
        if isinstance(existing, str) and existing:
            return existing
        new_id = _generate_dashboard_id()
        rb[_DASHBOARD_ID_KEY] = new_id
        return new_id
