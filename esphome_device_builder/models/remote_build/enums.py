"""Enums shared across the remote-build models."""

from __future__ import annotations

from enum import StrEnum


class RemoteBuildPeerSource(StrEnum):
    """
    How a peer dashboard ended up in the discovered-hosts surface.

    Today the only source is ``mdns`` — discovered via the
    ``_esphomebuilder._tcp.local.`` browse. Cross-subnet pair
    flows bypass discovery entirely and go straight through
    ``request_pair``, so no manual-host enum member exists.
    """

    MDNS = "mdns"


class PeerStatus(StrEnum):
    """
    Lifecycle state of a :class:`StoredPeer` row.

    PENDING rows accept only ``intent="pair_status"`` polls at
    the peer-link auth gate; APPROVED rows accept every intent.
    No explicit REJECTED state — a rejected request deletes the
    row.
    """

    PENDING = "pending"
    APPROVED = "approved"


class PeerLinkIntent(StrEnum):
    """
    Wire ``intent`` discriminator on the peer-link Noise WS msg1 payload.

    Sent cleartext (Noise XX msg1 isn't encrypted yet); sensitive
    fields (``dashboard_id``, ``label``) wait until the
    encrypted-under-finalised-cipher msg3.

    * ``PREVIEW`` — capture the receiver's pubkey for OOB pin
      verification; doesn't mutate receiver state.
    * ``PAIR_REQUEST`` — creates / refreshes a PENDING
      :class:`StoredPeer` row and fires
      ``REMOTE_BUILD_PAIR_REQUEST_RECEIVED``. Gated by the
      pairing window.
    * ``PEER_LINK`` — establishes the long-lived peer-link
      session for an APPROVED peer.
    * ``PAIR_STATUS`` — informational poll for a previously-
      submitted pair_request's current state.
    """

    PREVIEW = "preview"
    PAIR_REQUEST = "pair_request"
    PEER_LINK = "peer_link"
    PAIR_STATUS = "pair_status"


class IntentResponse(StrEnum):
    """
    Wire ``intent_response`` value the receiver returns over the peer-link.

    Sent in the post-handshake transport frame. ``StrEnum`` so
    members serialise to their wire string verbatim.

    * ``OK`` — preview captured pubkey; or peer_link accepted
      (caller keeps the WS open).
    * ``APPROVED`` — pair_status / pair_request on an
      already-APPROVED peer.
    * ``PENDING`` — pair_request created or refreshed a PENDING
      row, or pair_status / peer_link saw a still-PENDING row.
    * ``REJECTED`` — unknown ``dashboard_id``, pin mismatch, or
      unknown ``intent``.
    * ``NO_PAIRING_WINDOW`` — pair_request arrived while the
      receiver-side pairing window is closed.
    """

    OK = "ok"
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    NO_PAIRING_WINDOW = "no_pairing_window"
