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

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .json import dumps as json_dumps
from .json import loads as json_loads

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
_METADATA_FILE = ".device-builder.json"
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

    dashboard_id = _load_dashboard_id(config_dir)
    if dashboard_id is None:
        dashboard_id = _generate_dashboard_id()
        _save_dashboard_id(config_dir, dashboard_id)

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

    dashboard_id = _load_dashboard_id(config_dir)
    if dashboard_id is None:
        dashboard_id = _generate_dashboard_id()
        _save_dashboard_id(config_dir, dashboard_id)

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
    a miss; both files have to be present and parse cleanly or the
    caller regenerates. Failures are logged at debug level rather
    than warning since "first start, files don't exist" is the
    common path through here.
    """
    if not cert_path.exists() or not key_path.exists():
        return None, None
    try:
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()
        # Verify both parse so a corrupted file doesn't silently
        # land in the returned identity. ``load_pem_x509_certificate``
        # and ``load_pem_private_key`` raise on malformed input.
        x509.load_pem_x509_certificate(cert_pem)
        serialization.load_pem_private_key(key_pem, password=None)
    except Exception:
        _LOGGER.debug(
            "Persisted cert / key at %s / %s failed to parse; regenerating",
            cert_path,
            key_path,
            exc_info=True,
        )
        return None, None
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


def _persist_cert_pair(cert_path: Path, key_path: Path, cert_pem: bytes, key_pem: bytes) -> None:
    """
    Write cert + key to disk, with the key at ``0600`` from the start.

    The key file is opened with ``O_WRONLY | O_CREAT | O_TRUNC`` plus
    ``mode=0o600`` so the bytes are never on disk at world-readable
    permissions. ``Path.write_bytes`` followed by ``Path.chmod`` would
    leave a window between the write and the chmod where another
    process on the host could read the key at the default ``umask``
    permissions; a backup tool snapshotting the config dir during that
    window would also capture the key at the wrong mode. Cert is
    public-by-design and stays at the default mode (caller's umask).

    Order matters: key first with the restrictive mode, then the
    cert. If a crash happens between, the next ``get_or_create_identity``
    call sees a partial state and regenerates from scratch (which is
    correct).
    """
    # umask is process-wide and not directly settable per-file; the
    # explicit ``os.open`` with ``mode=`` parameter handles it on
    # POSIX. On Windows the mode argument is largely ignored, but
    # Windows' default ACL on a user's home dir is already
    # restrictive, so the practical risk window doesn't exist there.
    fd = os.open(
        key_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        _KEY_MODE,
    )
    try:
        os.write(fd, key_pem)
    finally:
        os.close(fd)
    # Re-apply the mode in case the file already existed: ``os.open``
    # with ``mode=`` is a *creation* mode, not an ``fchmod``. A
    # pre-existing key file with looser permissions would otherwise
    # keep them after the open call. Cheap and idempotent.
    os.chmod(key_path, _KEY_MODE)
    cert_path.write_bytes(cert_pem)


def _cert_fingerprint(cert_pem: bytes) -> str:
    """SHA-256 fingerprint of the DER-encoded cert as lowercase hex."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def _generate_dashboard_id() -> str:
    """Return a random base64url string identifying this dashboard installation."""
    return secrets.token_urlsafe(_DASHBOARD_ID_BYTES)


def _load_dashboard_id(config_dir: Path) -> str | None:
    """
    Read ``_remote_build.dashboard_id`` from the metadata sidecar.

    Returns ``None`` when the sidecar is missing, malformed, or
    doesn't carry the key; the caller will then generate and
    persist a fresh one. Doesn't go through
    :func:`controllers.config.metadata_transaction` because this
    runs at dashboard-start time before the controllers are wired,
    and a non-locking read of a startup-only file is fine.
    """
    metadata_path = config_dir / _METADATA_FILE
    if not metadata_path.exists():
        return None
    try:
        data = json_loads(metadata_path.read_bytes())
    except Exception:
        _LOGGER.debug(
            "Metadata sidecar at %s failed to parse for dashboard_id",
            metadata_path,
            exc_info=True,
        )
        return None
    if not isinstance(data, dict):
        return None
    rb = data.get(_REMOTE_BUILD_KEY)
    if not isinstance(rb, dict):
        return None
    value = rb.get(_DASHBOARD_ID_KEY)
    return value if isinstance(value, str) and value else None


def _save_dashboard_id(config_dir: Path, dashboard_id: str) -> None:
    """
    Persist ``dashboard_id`` into the metadata sidecar.

    Read-modify-write so we don't clobber other ``_remote_build``
    sub-keys (e.g. ``enabled`` and ``manual_hosts`` from phase 2 /
    2b). A bare write would replace the whole ``_remote_build``
    blob and silently reset everything else to defaults.
    """
    metadata_path = config_dir / _METADATA_FILE
    data: dict = {}
    if metadata_path.exists():
        try:
            loaded = json_loads(metadata_path.read_bytes())
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    rb = data.get(_REMOTE_BUILD_KEY)
    if not isinstance(rb, dict):
        rb = {}
        data[_REMOTE_BUILD_KEY] = rb
    rb[_DASHBOARD_ID_KEY] = dashboard_id
    metadata_path.write_bytes(json_dumps(data))
