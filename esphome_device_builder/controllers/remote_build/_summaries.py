"""
Wire-view projections for the remote-build controller.

Each helper turns a storage-shape dataclass
(:class:`StoredPeer`, :class:`StoredPairing`,
:class:`DashboardIdentity`) into the matching wire-view shape
(:class:`PeerSummary`, :class:`PairingSummary`,
:class:`IdentityView`) the WS layer renders. The projections
drop secret-equivalent bytes (raw ``static_x25519_pub``) and
fold in snapshot-time state the storage row itself doesn't
carry (``connected`` / ``connecting`` / ``last_connect_error``
read off the controller's RAM-canonical registries by the
caller).

Module-level rather than controller methods so the registries
aren't directly reachable from here; callers dereference and
thread the values through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from esphome.const import __version__ as esphome_version

from ...constants import __version__ as server_version
from ...models import (
    IdentityView,
    PairingSummary,
    PeerStatus,
    PeerSummary,
    StoredPairing,
    StoredPeer,
)

if TYPE_CHECKING:
    from ...helpers.dashboard_identity import DashboardIdentity


def peer_summary(peer: StoredPeer, *, status: PeerStatus, connected: bool) -> PeerSummary:
    """Project a :class:`StoredPeer` to wire :class:`PeerSummary`.

    Drops the raw ``static_x25519_pub`` bytes; ``pin_sha256`` is
    the wire-friendly form UIs render for OOB-verification, and
    the pubkey is only needed server-side to look up the peer
    against an incoming Noise handshake. ``status`` is supplied
    by the caller because :class:`StoredPeer` itself doesn't
    carry one — pending peers live in the controller's
    in-memory dict and persisted peers are implicitly approved.

    ``connected`` is the snapshot-time read the caller passes
    in. The intended source is
    ``dashboard_id in controller._peer_link_sessions`` (the
    RAM-canonical receiver-side session registry the peer-link
    handshake populates). PENDING callers always pass
    ``False``; a future code path that legitimately tracks
    connection state on a non-APPROVED row passes the bool
    through explicitly rather than inheriting a silent default.
    """
    return PeerSummary(
        dashboard_id=peer.dashboard_id,
        pin_sha256=peer.pin_sha256,
        label=peer.label,
        paired_at=peer.paired_at,
        status=status,
        peer_ip=peer.peer_ip,
        connected=connected,
    )


def pairing_summary(
    pairing: StoredPairing,
    *,
    connected: bool,
    connecting: bool = False,
    last_connect_error: str = "",
) -> PairingSummary:
    """Project a :class:`StoredPairing` to wire :class:`PairingSummary`.

    Mirror of :func:`peer_summary` for the offloader side.
    Drops the raw ``static_x25519_pub`` bytes. ``status`` reads
    off the row — the unified in-RAM ``_pairings`` dict carries
    both PENDING and APPROVED rows, with the disk filter
    stripping PENDING at serialise time.

    ``connected``, ``connecting``, and ``last_connect_error``
    are the snapshot-time reads the caller passes in. Intended
    sources:

    * ``connected``: ``pairing.pin_sha256 in
      controller._open_peer_links``, the RAM-canonical set the
      controller maintains from :class:`PeerLinkClient`-fired
      :attr:`EventType.OFFLOADER_PEER_LINK_OPENED` /
      :attr:`EventType.OFFLOADER_PEER_LINK_CLOSED` events.
    * ``connecting`` / ``last_connect_error``: the matching
      :class:`PeerLinkClient`'s
      :attr:`~PeerLinkClient.is_connecting` /
      :attr:`~PeerLinkClient.last_connect_error` properties.

    PENDING callers pass ``connected=False`` and let
    ``connecting`` / ``last_connect_error`` take their defaults
    — the offloader doesn't spawn a peer-link client until the
    receiver flips the row to APPROVED.
    """
    return PairingSummary(
        receiver_hostname=pairing.receiver_hostname,
        receiver_port=pairing.receiver_port,
        pin_sha256=pairing.pin_sha256,
        label=pairing.label,
        paired_at=pairing.paired_at,
        status=pairing.status,
        connected=connected,
        connecting=connecting,
        last_connect_error=last_connect_error,
        esphome_version=pairing.esphome_version,
        enabled=pairing.enabled,
    )


def identity_view(identity: DashboardIdentity, *, listener_bound: bool) -> IdentityView:
    """Project a :class:`DashboardIdentity` into the wire shape.

    The listener-bound bool comes from the dashboard's
    :meth:`DeviceBuilder.is_remote_build_listener_bound`
    accessor; surfaced on the view so the receiver-side
    Settings UI can branch on "this dashboard advertises
    itself" without re-checking the bind state on every
    paint.
    """
    return IdentityView(
        dashboard_id=identity.dashboard_id,
        pin_sha256=identity.pin_sha256,
        server_version=server_version,
        esphome_version=esphome_version,
        listener_bound=listener_bound,
    )
