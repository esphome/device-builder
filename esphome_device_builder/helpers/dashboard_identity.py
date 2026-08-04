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
  in the metadata sidecar's own ``_dashboard_identity`` block.
  Purely a correlation token (it appears in mDNS TXT, peer-link
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
from typing import Any

from ..controllers.config import metadata_transaction
from .async_ import run_in_executor
from .peer_link_identity import PeerLinkIdentity, PeerLinkIdentityStore

_DASHBOARD_ID_BYTES = 24
_DASHBOARD_IDENTITY_KEY = "_dashboard_identity"
_DASHBOARD_ID_KEY = "dashboard_id"
# Legacy location the id co-lived in with the receiver settings;
# read once to migrate it into ``_dashboard_identity``.
_LEGACY_REMOTE_BUILD_KEY = "_remote_build"

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


async def get_or_create_identities(
    config_dir: Path, identity_store: PeerLinkIdentityStore
) -> tuple[PeerLinkIdentity, DashboardIdentity]:
    """Load the peer-link keypair + composed dashboard identity in one shot."""
    peer_link = await identity_store.async_load()
    dashboard_id = await run_in_executor(_get_or_create_dashboard_id, config_dir)
    return peer_link, DashboardIdentity(
        dashboard_id=dashboard_id,
        pin_sha256=peer_link.pin_sha256,
    )


async def get_or_create_identity(
    config_dir: Path, identity_store: PeerLinkIdentityStore
) -> DashboardIdentity:
    """Return just the :class:`DashboardIdentity` half (UI / WS callers)."""
    _, dashboard = await get_or_create_identities(config_dir, identity_store)
    return dashboard


async def rotate_identity(
    config_dir: Path, identity_store: PeerLinkIdentityStore
) -> DashboardIdentity:
    """
    Mint a fresh X25519 peer-link keypair, preserving ``dashboard_id``.

    Every paired peer pinned on the old ``pin_sha256`` sees a
    fingerprint mismatch on the next Noise handshake and has
    to re-pair; ``dashboard_id`` survives so the audit trail
    stays readable across rotations.
    """
    peer_link = await identity_store.async_rotate()
    dashboard_id = await run_in_executor(_get_or_create_dashboard_id, config_dir)
    return DashboardIdentity(
        dashboard_id=dashboard_id,
        pin_sha256=peer_link.pin_sha256,
    )


def get_or_create_dashboard_id(config_dir: Path) -> str:
    """Sync accessor for the persistent ``dashboard_id`` (mints + persists on first call)."""
    return _get_or_create_dashboard_id(config_dir)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get_or_create_dashboard_id(config_dir: Path) -> str:
    """Return the persistent ``dashboard_id``, migrating a legacy copy or minting one."""
    with metadata_transaction(config_dir) as data:
        # Sweep any legacy co-tenant first so a stale copy can't
        # linger even when the new block already answers the read.
        legacy_id = _migrate_legacy_dashboard_id(data)
        block = data.get(_DASHBOARD_IDENTITY_KEY)
        if isinstance(block, dict):
            existing = block.get(_DASHBOARD_ID_KEY)
            if isinstance(existing, str) and existing:
                return existing
        else:
            block = {}
            data[_DASHBOARD_IDENTITY_KEY] = block
        new_id = legacy_id or secrets.token_urlsafe(_DASHBOARD_ID_BYTES)
        block[_DASHBOARD_ID_KEY] = new_id
        return new_id


def _migrate_legacy_dashboard_id(data: dict[str, Any]) -> str | None:
    """Pop a legacy ``_remote_build.dashboard_id``, dropping a block left empty by the pop."""
    legacy = data.get(_LEGACY_REMOTE_BUILD_KEY)
    if not isinstance(legacy, dict):
        return None
    value = legacy.pop(_DASHBOARD_ID_KEY, None)
    if not legacy:
        del data[_LEGACY_REMOTE_BUILD_KEY]
    return value if isinstance(value, str) and value else None
