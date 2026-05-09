"""
Persistent dashboard identity for the remote-build feature.

Generates and persists, on first call to
:func:`get_or_create_identity`:

* a long-lived self-signed TLS cert at
  ``<config_dir>/.device-builder-cert.pem``
* the matching private key at
  ``<config_dir>/.device-builder-key.pem`` (mode ``0600``)
* a stable random ``dashboard_id`` (24 bytes of entropy,
  base64url) in the metadata sidecar's ``_remote_build`` block

Subsequent calls reload the same bytes. Cert validity is 100
years; rotation is explicit via :func:`rotate_certificate`.

Generation is an Ed25519 keypair plus a few file writes on the
first call. Sync and blocking; async callers must hop through
``run_in_executor``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..controllers.config import metadata_transaction
from .atomic_io import atomic_write

_LOGGER = logging.getLogger(__name__)

_CERT_FILENAME = ".device-builder-cert.pem"
_KEY_FILENAME = ".device-builder-key.pem"
_KEY_MODE = 0o600
_DASHBOARD_ID_BYTES = 24
_CERT_VALIDITY_YEARS = 100  # rotation is explicit; never driven by expiry
_CERT_NOT_BEFORE_BACKDATE = timedelta(minutes=5)  # tolerate small peer-clock skew
_CERT_COMMON_NAME = "ESPHome Device Builder"
_REMOTE_BUILD_KEY = "_remote_build"
_DASHBOARD_ID_KEY = "dashboard_id"

# Serialise the load -> generate -> persist path so two callers
# racing on first-time creation don't waste CPU on parallel
# keypair generation and don't interleave atomic writes onto the
# same cert / key files. Production calls this once at startup;
# the lock handles the test-style 4-thread race and any future
# caller that picks up the helper.
_IDENTITY_LOCK = threading.Lock()


@dataclass(frozen=True)
class DashboardIdentity:
    """The persistent identity for one dashboard installation."""

    dashboard_id: str
    cert_pem: bytes
    key_pem: bytes
    # SHA-256 of the cert's SubjectPublicKeyInfo (same input as
    # RFC 7469 / HPKP, but encoded as lowercase hex rather than
    # the RFC's base64 because hex matches what TLS / cert UIs
    # display when users compare fingerprints out-of-band).
    # Pinning the public key (not the whole cert) lets cert
    # metadata refresh without invalidating paired peers as long
    # as the keypair stays the same.
    pin_sha256: str

    @property
    def pin_sha256_formatted(self) -> str:
        """Return the pin as space-separated byte pairs for display."""
        return " ".join(self.pin_sha256[i : i + 2] for i in range(0, len(self.pin_sha256), 2))


def get_or_create_identity(config_dir: Path) -> DashboardIdentity:
    """
    Load the persistent identity, generating it on first call.

    Idempotent. A partial / unparsable state (one half missing, or
    PEM that fails to parse, or cert/key mismatch) is treated as
    "missing" and regenerated; paired peers then re-pair against
    the new fingerprint, which is the right user-visible outcome
    when on-disk identity has gone wrong.
    """
    cert_path = config_dir / _CERT_FILENAME
    key_path = config_dir / _KEY_FILENAME

    with _IDENTITY_LOCK:
        cert_pem, key_pem = _load_cert_pair(cert_path, key_path)
        if cert_pem is None or key_pem is None:
            cert_pem, key_pem = _generate_cert_pair()
            _persist_cert_pair(cert_path, key_path, cert_pem, key_pem)
            _LOGGER.info("Generated new dashboard identity at %s", cert_path)

    dashboard_id = _get_or_create_dashboard_id(config_dir)

    return DashboardIdentity(
        dashboard_id=dashboard_id,
        cert_pem=cert_pem,
        key_pem=key_pem,
        pin_sha256=_spki_fingerprint(cert_pem),
    )


def rotate_certificate(config_dir: Path) -> DashboardIdentity:
    """
    Generate a fresh cert + key, replacing whatever's on disk.

    Keeps the existing ``dashboard_id`` (stable identity across
    rotations; only the cert changes). Every paired peer will see
    a fingerprint mismatch on the next connection and need to
    re-pair.
    """
    cert_path = config_dir / _CERT_FILENAME
    key_path = config_dir / _KEY_FILENAME
    with _IDENTITY_LOCK:
        cert_pem, key_pem = _generate_cert_pair()
        _persist_cert_pair(cert_path, key_path, cert_pem, key_pem)
        _LOGGER.info("Rotated dashboard identity at %s", cert_path)

    dashboard_id = _get_or_create_dashboard_id(config_dir)

    return DashboardIdentity(
        dashboard_id=dashboard_id,
        cert_pem=cert_pem,
        key_pem=key_pem,
        pin_sha256=_spki_fingerprint(cert_pem),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_cert_pair(cert_path: Path, key_path: Path) -> tuple[bytes | None, bytes | None]:
    """
    Read the persisted cert + key, returning ``(None, None)`` on any miss.

    Both files must exist, parse cleanly, AND the cert's public
    key must match the private key's; mismatched pairs (after a
    manual rotation of one half, or a backup-restore reassembling
    mismatched files) would otherwise fail at TLS handshake time
    with an opaque "key values mismatch".
    """
    if not cert_path.exists() or not key_path.exists():
        return None, None
    try:
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        private_key = serialization.load_pem_private_key(key_pem, password=None)
        # Compare via SPKI bytes (key-type-agnostic; ed25519 has no
        # public_numbers()) to reject a stale key paired with a
        # fresh cert before TLS handshake.
        spki = serialization.PublicFormat.SubjectPublicKeyInfo
        der = serialization.Encoding.DER
        if cert.public_key().public_bytes(der, spki) != private_key.public_key().public_bytes(
            der, spki
        ):
            _LOGGER.warning(
                "Persisted cert at %s does not match private key at %s; regenerating",
                cert_path,
                key_path,
            )
            return None, None
    except Exception:
        _LOGGER.warning(
            "Persisted cert / key at %s / %s failed to parse; regenerating",
            cert_path,
            key_path,
            exc_info=True,
        )
        return None, None
    return cert_pem, key_pem


def _generate_cert_pair() -> tuple[bytes, bytes]:
    """Generate a fresh Ed25519 keypair and a self-signed cert."""
    key = ed25519.Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _CERT_COMMON_NAME)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CERT_NOT_BEFORE_BACKDATE)
        .not_valid_after(_add_years(now, _CERT_VALIDITY_YEARS))
        # SAN deliberately minimal (localhost only). Paired peers
        # pin on the SPKI fingerprint and don't run hostname
        # validation, so non-matching server names (homeassistant.local,
        # host IPs) handshake fine. A non-pinning client would see a
        # hostname-mismatch warning, which is the right outcome since
        # this isn't a publicly-trusted cert.
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=True,
        )
        # Ed25519 self-signs without a separate hash algorithm
        # (signature includes the digest internally).
        .sign(key, None)
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _persist_cert_pair(cert_path: Path, key_path: Path, cert_pem: bytes, key_pem: bytes) -> None:
    """
    Write cert + key atomically, with the key at ``0600``.

    Key first; if cert write fails after, the load-time cross-check
    rejects the half-pair and regenerates from scratch.
    """
    atomic_write(key_path, key_pem, mode=_KEY_MODE)
    atomic_write(cert_path, cert_pem)


def compute_spki_fingerprint(cert: x509.Certificate) -> str:
    """
    Return the SHA-256 of the cert's SubjectPublicKeyInfo as lowercase hex.

    Pins the public key, not the whole cert, so reissuing with the
    same keypair (e.g. to refresh metadata) doesn't invalidate
    paired peers.

    Public so future cert-pinning consumers can reuse it directly
    against an already-parsed :class:`x509.Certificate` rather
    than going through the PEM-input wrapper.
    """
    spki_der = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki_der).hexdigest()


def _spki_fingerprint(cert_pem: bytes) -> str:
    """PEM-input wrapper around :func:`compute_spki_fingerprint`."""
    return compute_spki_fingerprint(x509.load_pem_x509_certificate(cert_pem))


def _generate_dashboard_id() -> str:
    """Return a random base64url string identifying this dashboard installation."""
    return secrets.token_urlsafe(_DASHBOARD_ID_BYTES)


def _add_years(d: datetime, years: int) -> datetime:
    """Return *d* shifted by *years*; clamps Feb 29 -> Feb 28 in non-leap targets."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _get_or_create_dashboard_id(config_dir: Path) -> str:
    """
    Return the persistent ``dashboard_id``, generating one if absent.

    The read-modify-write runs under the metadata-sidecar lock so
    the "exists?" check and the "generate + persist" step are
    atomic against any concurrent ``_remote_build`` mutation.
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
