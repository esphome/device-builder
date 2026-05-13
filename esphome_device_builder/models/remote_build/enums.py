"""Enums shared across the remote-build models."""

from __future__ import annotations

from enum import StrEnum


class RemoteBuildPeerSource(StrEnum):
    """
    How a peer dashboard ended up in the discovered-hosts surface.

    The discovered-hosts surface is
    :meth:`OffloaderController.hosts_snapshot` (sync read used
    by ``subscribe_events.initial_state.hosts``) plus the
    matching ``REMOTE_BUILD_HOST_ADDED`` /
    ``REMOTE_BUILD_HOST_REMOVED`` events.

    Today the only source is ``mdns`` — discovered via the
    ``_esphomebuilder._tcp.local.`` browse. The enum stays as a
    discriminator on :class:`RemoteBuildPeer` for cross-subnet
    pair flows that bypass mDNS by typing the hostname / port
    directly into ``request_pair`` (no intermediate "save this
    host" step needed; the pair either succeeds or doesn't).
    """

    MDNS = "mdns"


class PeerStatus(StrEnum):
    """
    Lifecycle state of a :class:`StoredPeer` row.

    ``PENDING``: an offloader's pair-request landed and the
    receiver's admin hasn't accepted yet. The peer-link auth
    gate lets a connection from this peer's pubkey complete the
    Noise handshake but only honours an ``intent="pair_status"``
    query; every other intent is rejected at the post-handshake
    dispatch.

    ``APPROVED``: admin clicked Accept. Full access — the auth
    gate looks up the offloader's static X25519 pubkey hash
    (extracted from the Noise XX handshake transcript) against
    this row on every connection.

    No explicit ``REJECTED`` terminal state — a rejected request
    deletes the row. If the same offloader retries, it lands as
    a fresh pending row and the admin chooses again. Avoids the
    bookkeeping a rejected-list would need; a future re-auth
    wizard can revisit if blocklisting becomes useful.
    """

    PENDING = "pending"
    APPROVED = "approved"


class PeerLinkIntent(StrEnum):
    """
    Wire ``intent`` discriminator on the peer-link Noise WS msg1 payload.

    Sent in cleartext on msg1 (Noise XX hasn't established a key
    yet for that frame's payload) so the receiver can route the
    session before the handshake completes. The sensitive
    metadata (``dashboard_id``, ``label``) waits until msg3,
    which is encrypted under the now-finalized cipher.

    * ``PREVIEW`` — capture the receiver's static pubkey for
      OOB pin verification. Doesn't change any receiver state.
    * ``PAIR_REQUEST`` — gated by the pairing window from #106
      design choice (c). Creates / refreshes a PENDING
      ``StoredPeer`` row and fires
      ``REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.
    * ``PEER_LINK`` — establishes a long-lived peer-link session
      for an already-APPROVED peer. The WS stays open for
      application messages (bundle upload, build, firmware
      download).
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

    Sent in the post-handshake transport frame after the Noise XX
    handshake completes. ``StrEnum`` so members serialise to their
    wire string verbatim through ``json.dumps`` and so equality
    comparisons against the raw string still work for callers that
    haven't migrated yet.

    Per-intent semantics (cross-referenced with #106 design choice
    (h)):

    * ``OK`` — success on ``intent="preview"`` (handshake captured
      pubkey, nothing else needed) or on ``intent="peer_link"``
      from an APPROVED peer (caller can keep the WS open for
      application messages).
    * ``APPROVED`` — ``intent="pair_status"`` poll observing an
      APPROVED row, or ``intent="pair_request"`` from a peer
      that's already APPROVED (we don't demote them; the offloader
      is expected to switch to ``intent="peer_link"``).
    * ``PENDING`` — ``intent="pair_request"`` created or refreshed
      a PENDING row, or ``intent="pair_status"`` /
      ``intent="peer_link"`` polled a row that's still PENDING.
    * ``REJECTED`` — unknown ``dashboard_id``, pin mismatch
      (handshake's pubkey doesn't match the stored row), or
      unknown ``intent``. The offloader's UI surfaces a
      "send a fresh pair_request" CTA.
    * ``NO_PAIRING_WINDOW`` — ``intent="pair_request"`` arrived
      while the receiver-side pairing window is closed; no row
      created. The offloader's UI prompts the user to ask the
      receiving dashboard's user to open the Pairing requests
      screen.
    """

    OK = "ok"
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    NO_PAIRING_WINDOW = "no_pairing_window"
