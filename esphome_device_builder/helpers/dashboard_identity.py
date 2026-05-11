"""
Persistent dashboard identity for the remote-build feature.

Bundles two persistent values that together identify one
dashboard installation to peer dashboards:

* ``pin_sha256`` — the SHA-256 fingerprint of the dashboard's
  X25519 peer-link public key. This is the value paired
  offloaders pin during the Noise XX handshake and the value
  mDNS TXT broadcasts in ``pin_sha256``. The keypair itself
  lives in :mod:`helpers.peer_link_identity`; this module
  composes its fingerprint with the dashboard_id below.
* ``dashboard_id`` — a stable random base64url string stored
  in the metadata sidecar's ``_remote_build`` block. Purely a
  correlation token (it appears in mDNS TXT, peer-link
  handshakes, audit logs, and pair-request records so a peer
  can recognise which dashboard it's talking to across
  rotations of the underlying cryptographic identity). NOT
  an authentication credential; pairing authenticates via
  the X25519 Noise XX handshake.

Pre-phase-4a-r2 this module also owned a separate Ed25519
self-signed cert used by an HTTPS+bearer peer-link surface
that's been retired (the listener now runs plain TCP + Noise
XX, see :mod:`controllers.remote_build.peer_link`). The cert
+ key files were dead artefacts after the pivot but the UI
kept reading their SPKI fingerprint, which produced a
fingerprint that didn't match what offloaders observed on
the wire. Rewriting this module to delegate to
:mod:`helpers.peer_link_identity` closes that gap.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..controllers.config import metadata_transaction
from .peer_link_identity import (
    get_or_create_peer_link_identity,
    rotate_peer_link_identity,
)

_DASHBOARD_ID_BYTES = 24
_REMOTE_BUILD_KEY = "_remote_build"
_DASHBOARD_ID_KEY = "dashboard_id"

# Public validation contract for ``dashboard_id`` strings on the
# wire. ``dashboard_id`` is generated via
# :func:`secrets.token_urlsafe` at :data:`_DASHBOARD_ID_BYTES`
# bytes of entropy (32 base64url chars at the current size).
# The cap of 64 defends against runaway inputs without
# rejecting legitimate values; the pattern catches probes
# carrying control bytes / non-printables. Both consumers
# (``controllers/remote_build`` for WS-command argument
# validation, and ``controllers/remote_build/peer_link`` for
# msg3-supplied values on the Noise WS) import these so a
# future entropy bump or alphabet change happens in one
# place.
DASHBOARD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
DASHBOARD_ID_MAX_CHARS = 64


@dataclass(frozen=True)
class DashboardIdentity:
    """
    The persistent identity for one dashboard installation.

    Composed of the X25519 peer-link key's ``pin_sha256``
    (what offloaders pin during pairing) and the stable
    ``dashboard_id`` correlation token. The X25519 private
    key itself is not held here — callers that need to drive
    the Noise handshake load
    :class:`~helpers.peer_link_identity.PeerLinkIdentity`
    directly.
    """

    dashboard_id: str
    pin_sha256: str

    @property
    def pin_sha256_formatted(self) -> str:
        """Return the pin as space-separated byte pairs for display."""
        return " ".join(self.pin_sha256[i : i + 2] for i in range(0, len(self.pin_sha256), 2))


def get_or_create_identity(config_dir: Path) -> DashboardIdentity:
    """
    Load the persistent identity, generating it on first call.

    Idempotent. Lazy-creates the X25519 peer-link keypair via
    :func:`get_or_create_peer_link_identity` and the
    ``dashboard_id`` token via the internal helper below; both
    are cheap repeat calls thereafter. The returned struct's
    ``pin_sha256`` is the SHA-256 of the peer-link public key —
    the same value the mDNS TXT advertises and the value
    paired offloaders pin against on the next Noise handshake.

    Thread-safety: this function holds no shared state of its
    own, so concurrent callers are serialised by the two
    underlying primitives' own locks
    (:data:`helpers.peer_link_identity._IDENTITY_LOCK` for the
    X25519 keypair file, and :func:`metadata_transaction`'s
    ``_METADATA_LOCK`` for the dashboard_id JSON write). The
    pre-rewrite module held its own
    :class:`threading.Lock` to guard the Ed25519 cert
    generation path; that lock is gone with the cert code,
    and the composition pattern here re-derives equivalent
    safety from the locks already present in the helpers it
    delegates to.
    """
    peer_link = get_or_create_peer_link_identity(config_dir)
    return DashboardIdentity(
        dashboard_id=_get_or_create_dashboard_id(config_dir),
        pin_sha256=peer_link.pin_sha256,
    )


def rotate_identity(config_dir: Path) -> DashboardIdentity:
    """
    Rotate the X25519 peer-link keypair, preserving ``dashboard_id``.

    Mints a fresh X25519 keypair via
    :func:`rotate_peer_link_identity` (replacing whatever's on
    disk). Every paired peer that pinned the old ``pin_sha256``
    will see a fingerprint mismatch on the next Noise handshake
    and need to re-pair, which is the right user-visible
    outcome when the operator deliberately rotates. The
    ``dashboard_id`` is intentionally preserved across
    rotations so the receiver-side audit trail stays readable.
    """
    peer_link = rotate_peer_link_identity(config_dir)
    return DashboardIdentity(
        dashboard_id=_get_or_create_dashboard_id(config_dir),
        pin_sha256=peer_link.pin_sha256,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get_or_create_dashboard_id(config_dir: Path) -> str:
    """
    Return the persistent ``dashboard_id``, generating one if absent.

    The read-modify-write runs under the metadata-sidecar lock
    so the "exists?" check and the "generate + persist" step
    are atomic against any concurrent ``_remote_build``
    mutation.
    """
    with metadata_transaction(config_dir) as data:
        rb = data.get(_REMOTE_BUILD_KEY)
        if not isinstance(rb, dict):
            rb = {}
            data[_REMOTE_BUILD_KEY] = rb
        existing = rb.get(_DASHBOARD_ID_KEY)
        if isinstance(existing, str) and existing:
            return existing
        new_id = secrets.token_urlsafe(_DASHBOARD_ID_BYTES)
        rb[_DASHBOARD_ID_KEY] = new_id
        return new_id
