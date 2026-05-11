"""Remote-build feature models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Literal, NotRequired, TypedDict

import voluptuous as vol
from mashumaro.mixins.orjson import DataClassORJSONMixin

from ..helpers.voluptuous_validators import lowercase_hex, not_bool


class RemoteBuildPeerSource(StrEnum):
    """
    How a peer dashboard ended up in the discovered-hosts surface.

    The discovered-hosts surface is
    :meth:`RemoteBuildController.hosts_snapshot` (sync read used
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
    bookkeeping a rejected-list would need; phase 8's re-auth
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
    * ``PEER_LINK`` — establishes a peer-link session for an
      already-APPROVED peer. Phase 5+ keeps the WS open for
      application messages (bundle upload, build, firmware
      download); part 4 just answers the handshake.
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
      application messages in phase 5+).
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


# ---------------------------------------------------------------------------
# Event payload shapes (TypedDict so the bus.fire data dict is type-checked at
# the call site without changing the wire shape; mirrors HA's
# ``EventStateChangedData`` / ``EventStateReportedData`` pattern).
# ---------------------------------------------------------------------------


class RemoteBuildIdentityRotatedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_IDENTITY_ROTATED``.

    Fires after ``rotate_identity`` persists the new X25519
    keypair to disk and attempts the listener rebuild. The
    event ONLY signals that the rotation landed on disk and
    the rebuild was attempted — not that the listener is
    currently serving traffic against the new key. The
    rebuild can fail-soft (port collision, permission denied,
    listener unbound at rotation time), in which case the
    rotater's ``IdentityView`` response carries
    ``listener_bound=False`` and the new pin will hit the
    wire on the next successful bind. Subscribers (the
    offloader-side peer-link, the receiver Settings UI) use
    the event to refresh their cached pin without polling
    ``get_identity``; they should check
    ``IdentityView.listener_bound`` (via a follow-up
    ``get_identity`` call or by inspecting their own session
    state) before assuming end-to-end propagation.
    """

    dashboard_id: str
    pin_sha256: str


class RemoteBuildPairRequestReceivedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.

    Fired by the peer-link Noise WS handler when a fresh
    ``intent="pair_request"`` arrives during an open pairing
    window. The receiver Settings UI surfaces the row in the
    Pairing requests inbox; ``peer_ip`` lets the operator
    sanity-check the source against expectations before
    OOB-confirming the pin.

    ``paired_at`` carries the receiver-clock unix timestamp at
    row creation, matching the ``StoredPeer.paired_at`` field
    on the in-memory PENDING entry. Lets a subscriber rendering
    the inbox from the event stream sort the most-recent attempt
    first without a follow-up snapshot read.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    peer_ip: str
    paired_at: float


class RemoteBuildPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED``.

    Fires from three paths:

    * ``approve_peer`` promoting a PENDING dict entry to
      APPROVED (``status="approved"``).
    * ``remove_peer`` dropping either a PENDING dict entry or
      an APPROVED list row (``status="removed"``).
    * Pairing-window-close clearing the in-memory PENDING dict
      (``status="removed"`` per cleared entry).

    The ``status="removed"`` event is what wakes any in-flight
    ``intent="pair_status"`` long-poll on a paired offloader so
    its listener task drops the offloader's local state.
    """

    dashboard_id: str
    status: Literal["approved", "removed"]


class OffloaderPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_STATUS_CHANGED``.

    Offloader-side counterpart to
    :class:`RemoteBuildPairStatusChangedData`. Fired on the
    offloader's local bus from two paths:

    * The per-row pair-status listener task
      (``RemoteBuildController._await_pair_status_flip`` →
      ``_apply_pair_status_result`` → ``_fire_offloader_pair_status_changed``)
      when a previously-PENDING :class:`StoredPairing` flips to
      ``APPROVED`` (admin clicked Accept) or is dropped because
      the receiver returned ``REJECTED`` (admin clicked Reject;
      window closed clearing the receiver-side dict; row never
      existed; pin rotated).
    * ``RemoteBuildController.unpair`` when the user removes a
      row, so other clients on the global ``subscribe_events``
      stream see the removal without re-fetching the pairings
      snapshot.

    Delivered to clients via the existing global
    ``subscribe_events`` stream — no separate subscription
    channel.

    Carries ``pin_sha256`` as the canonical identifier (4a-o
    part 6 re-keyed offloader-side state on pin instead of
    ``(hostname, port)``); receiver coords stay on the payload
    as display fields the frontend can show without a
    follow-up lookup.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    status: Literal["approved", "removed"]


class OffloaderPairEndpointReboundData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND``.

    Fired by the offloader's mDNS auto-rebind path
    (``RemoteBuildController._probe_and_rebind_endpoint``) after
    a paired receiver's broadcast arrived from a different
    ``(hostname, port)`` than the ``StoredPairing`` records and a
    probe-before-mutate Noise XX handshake against the new
    endpoint confirmed the responder's static pubkey hash still
    matches the stored ``pin_sha256``.

    Carries the row's stable ``pin_sha256`` plus the new
    receiver coordinates so subscribers update display fields
    without a follow-up snapshot read. The peer-link client task
    has already been respawned against the new coordinates by
    the time this event fires; the ``OFFLOADER_PEER_LINK_OPENED``
    fired by the new client follows in the same loop tick after
    the handshake completes.
    """

    pin_sha256: str
    receiver_hostname: str
    receiver_port: int


class OffloaderPairPinMismatchData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_PIN_MISMATCH``.

    Fired by the offloader's per-row pair-status listener task
    (``RemoteBuildController._apply_pair_status_result``) when a
    Noise XX handshake to the receiver returns
    ``IntentResponse.APPROVED`` but the observed
    ``pin_sha256`` (lowercase-hex SHA-256 of the receiver's
    static X25519 pubkey, captured from the handshake
    transcript) doesn't match what the offloader stored at pair
    time on :class:`StoredPairing.pin_sha256`. The receiver's
    identity rotated under us (legitimate
    ``rotate_peer_link_identity`` from the receiver-side admin,
    or someone replacing the receiver).

    Fires *alongside* ``OFFLOADER_PAIR_STATUS_CHANGED
    status="removed"`` (the row drops either way), but carries
    the diagnostic detail (``expected_pin`` /
    ``observed_pin``) the status-changed event doesn't, plus
    the offloader's local ``receiver_label`` so the alert can
    name the row even after the pairings list has dropped it.
    The frontend's 4b-4 alert plumbing reshape uses the
    distinct event to surface a "re-pair to confirm the new
    identity" CTA, separate from the peer-revocation case.

    No receiver-side counterpart event; the receiver never sees
    its own pin drift, and the symmetric "offloader rotated"
    case lands as a fresh PENDING row on the receiver's inbox
    via :attr:`EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED`.

    ``pin_sha256`` is the **stored** pin the row was keyed on
    (same value as ``expected_pin``); duplicated as a separate
    field so the controller's listener has a direct primary
    key for ``_offloader_alerts`` lookup without parsing
    ``expected_pin``.
    """

    receiver_hostname: str
    receiver_port: int
    receiver_label: str
    pin_sha256: str
    expected_pin: str
    observed_pin: str


class OffloaderPairAlertDismissedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_ALERT_DISMISSED``.

    Fired when an entry leaves the controller's RAM-only
    ``_offloader_alerts`` dict via one of the two resolution
    paths that fix the underlying broken state: a successful
    ``request_pair`` against the same ``(hostname, port)``
    (re-pair auto-resolved the alert), or ``unpair`` removing
    the row outright. There is no operator-driven dismiss
    surface; clicking "OK got it" without acting would just
    hide a broken pairing the next peer-link session would
    still fail against, so re-pair and unpair are the only
    ways the alert clears. The event lets other tabs / clients
    on the global ``subscribe_events`` stream sync their local
    alerts list without re-fetching the snapshot.

    Carries ``pin_sha256`` as the canonical row identifier (4a-o
    part 6 re-keyed `_offloader_alerts` on pin); receiver
    coordinates stay on the payload as display fields. No
    discriminator on *which* resolution path got us here — the
    user-facing outcome (the alert disappears) is the same
    either way.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str


class OffloaderPinMismatchAlert(TypedDict):
    """
    Snapshot row in the offloader-side alerts list.

    Mirror of :class:`OffloaderPairPinMismatchData` (the live
    event) plus a ``kind`` discriminator so a single alerts
    list can carry both pin-mismatch and peer-revoked entries
    on the wire. Frontend subscribers branch on ``kind`` to
    pick the alert copy + CTA.

    ``fired_at`` is the wall-clock unix timestamp the alert
    was added to the dict. The snapshot's order is dict
    insertion order: a brand-new row appends at the tail; an
    upsert on an existing key keeps that key's slot in place
    (Python dict semantics) so a re-fire on the same row
    doesn't reshuffle the snapshot. Frontends that want
    "newest first" sort on ``fired_at`` themselves.
    """

    kind: Literal["pin_mismatch"]
    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    receiver_label: str
    expected_pin: str
    observed_pin: str
    fired_at: float


class OffloaderPeerRevokedAlert(TypedDict):
    """
    Snapshot row in the offloader-side alerts list.

    Mirror of :class:`OffloaderPairPeerRevokedData` plus the
    ``kind`` discriminator. Same shape rationale as
    :class:`OffloaderPinMismatchAlert`.
    """

    kind: Literal["peer_revoked"]
    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    receiver_label: str
    fired_at: float


# Sum type the snapshot list carries. Each entry is one of
# the two TypedDicts above; the ``kind`` Literal narrows
# field access at the consumer.
OffloaderAlertSnapshotEntry = OffloaderPinMismatchAlert | OffloaderPeerRevokedAlert


class OffloaderPairPeerRevokedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_PEER_REVOKED``.

    Fired by the offloader's per-row pair-status listener task
    (``RemoteBuildController._apply_pair_status_result``) when
    a Noise XX handshake to the receiver returns
    ``IntentResponse.REJECTED`` for a row the offloader had as
    PENDING / APPROVED. The receiver-side admin clicked Reject,
    the pairing window closed clearing the receiver's pending
    dict, the offloader's own peer-link identity rotated, or
    the receiver simply doesn't have this row (legitimate
    receiver re-install). From the offloader's perspective all
    four collapse to "the receiver isn't going to talk to us";
    the alert copy stays generic ("the receiver removed us;
    reach out to that admin if this was a mistake").

    Fires *alongside* ``OFFLOADER_PAIR_STATUS_CHANGED
    status="removed"``; the distinct event lets the frontend's
    4b-4 alert plumbing reshape surface a different CTA
    ("contact the receiver admin") versus a pin-mismatch alert
    ("re-pair right now to pick up the new identity"). The
    operator action differs.

    The ``receiver_label`` is carried so the alert can name the
    row even after the pairings list has dropped it.
    ``pin_sha256`` carries the row's primary key (4a-o part 6
    re-keyed offloader-side state on pin) so the controller's
    listener has a direct lookup. No extra diagnostic detail;
    the receiver doesn't tell us *why* REJECTED, and the
    offloader can't distinguish admin-Reject from window-close
    from row-never-existed at this layer.
    """

    receiver_hostname: str
    receiver_port: int
    receiver_label: str
    pin_sha256: str


class RemoteBuildPairingWindowChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED``.

    Fires whenever the in-process pairing window opens, extends,
    or closes. ``expires_in_seconds`` is ``None`` when ``open`` is
    ``False``; otherwise it's the remaining lifetime against the
    latest extend, which the receiver-side frontend renders as a
    live countdown.
    """

    open: bool
    expires_in_seconds: float | None


class RemoteBuildHostAddedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_HOST_ADDED``.

    Carries the full :class:`RemoteBuildPeer` projection of an
    mDNS-discovered (or refreshed) peer dashboard. Fires from
    :meth:`RemoteBuildController._on_service_state_change`'s
    cache-hit branch and the asynchronous
    :meth:`_resolve_and_apply` resolve-success branch. Upsert
    semantics — the frontend keys the discovered-hosts list on
    ``name`` (the mDNS service-instance name) and replaces an
    existing row with the same key when this event fires.
    """

    name: str
    hostname: str
    port: int
    source: str
    addresses: list[str]
    server_version: str
    esphome_version: str


class RemoteBuildHostRemovedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_HOST_REMOVED``.

    Fires when zeroconf delivers a ``Removed`` event (TTL expiry
    without renewal, or an explicit goodbye). ``name`` matches
    the ``name`` field of the corresponding
    :class:`RemoteBuildHostAddedData`.
    """

    name: str


class OffloaderPeerLinkOpenedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PEER_LINK_OPENED``.

    Fired by an offloader-side :class:`PeerLinkClient` once its
    long-lived peer-link session reaches the post-handshake
    ``intent_response: ok`` state and the dispatch loop is
    parked waiting for application frames. Subscribers (the
    offloader's frontend Settings UI) update the
    per-receiver "connected" indicator on this event.

    ``pin_sha256`` is the canonical offloader-side row key
    (4a-o part 6 re-keyed offloader state on pin); receiver
    coords stay on the payload as display fields the frontend
    can render without a follow-up lookup.

    ``esphome_version`` is the receiver's
    :data:`esphome.const.__version__` lifted off the
    ``intent_response`` payload. Empty when the receiver's
    response didn't carry the field (older receiver predating
    this wire change, or any future intent that doesn't include
    it). The controller subscribes to this event to refresh
    :attr:`StoredPairing.esphome_version` so pick_build_path's
    version-compat gate sees the up-to-date value on the next
    decision.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    esphome_version: str


class OffloaderPeerLinkClosedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PEER_LINK_CLOSED``.

    Fires whenever a peer-link client's session ends — clean
    receiver-driven ``terminate``, heartbeat timeout,
    transport error, or the controller cancelling the task on
    ``unpair`` / shutdown. ``reason`` carries the wire value
    from the receiver-side :class:`TerminateReason` enum when
    the close came from a structured ``terminate`` frame
    (``"superseded"`` / ``"server_shutting_down"`` /
    ``"heartbeat_timeout"`` / ``"malformed_frame"``), or an
    offloader-side reason when our side initiated:
    ``"transport_error"`` / ``"heartbeat_timeout"`` /
    ``"client_stopped"`` / ``"peer_hung_up"`` /
    ``"auth_rejected"`` / ``"pin_mismatch"``. The
    :class:`PeerLinkClient`'s reconnect logic branches on this
    — a ``"superseded"`` close means a newer offloader
    instance with the same ``dashboard_id`` already took our
    slot, so reconnecting would just collide; the client
    orphans rather than retrying.

    ``error_detail`` is a one-line human-readable description
    of the underlying failure for the categories that have one
    (transport / Noise exceptions; auth-rejected handshakes;
    pin-mismatch). Empty for clean closes where the category
    *is* the explanation (``"client_stopped"``, ``"superseded"``,
    receiver-driven ``terminate`` frames). The frontend displays
    the detail under the paired-row's "Last connection error"
    line so the operator sees "ConnectionRefusedError: [Errno
    61] Connection refused" instead of just the
    ``transport_error`` category.

    ``pin_sha256`` is the canonical offloader-side row key
    (4a-o part 6 re-keyed offloader state on pin); receiver
    coords stay on the payload as display fields.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    reason: str
    error_detail: str


class ReceiverPeerLinkSessionOpenedData(TypedDict):
    """
    Payload for ``EventType.RECEIVER_PEER_LINK_SESSION_OPENED``.

    Fired by :meth:`RemoteBuildController.register_peer_link_session`
    after the receiver has installed an offloader's peer-link
    Noise WS session in its ``_peer_link_sessions`` registry —
    i.e. the post-handshake ``_run_peer_link_session`` is about
    to enter its dispatch loop. Receiver-side counterpart to
    :class:`OffloaderPeerLinkOpenedData`.

    ``dashboard_id`` is the offloader's stable identity captured
    from the Noise XX handshake transcript.
    """

    dashboard_id: str


class ReceiverPeerLinkSessionClosedData(TypedDict):
    """
    Payload for ``EventType.RECEIVER_PEER_LINK_SESSION_CLOSED``.

    Fired by :meth:`RemoteBuildController.unregister_peer_link_session`
    when the receiver's session loop unwinds (offloader
    disconnect, heartbeat timeout, controller shutdown,
    ``superseded`` eviction). Receiver-side counterpart to
    :class:`OffloaderPeerLinkClosedData` — but no ``reason``
    field, because the receiver only sees "the loop returned"
    and the rich reason classification (transport vs heartbeat
    vs structured terminate) lives on the offloader side where
    those branches diverge.
    """

    dashboard_id: str


class QueueStatusFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.QUEUE_STATUS``.

    Wire shape sent by the receiver-side
    :class:`RemoteBuildController` over an active peer-link
    session whenever the firmware queue transitions
    (``JOB_QUEUED`` / ``JOB_STARTED`` / terminal events).
    Encrypted under the established Noise session and
    serialised as JSON before going on the wire.

    The three fields aren't strictly redundant: the
    ``running=False, queue_depth>0`` window exists between
    ``await _queue.put(job)`` and the runner's ``_queue.get()``
    landing the same item, so a phase-7 scheduler that reads
    only ``running`` would misclassify a fully-loaded receiver
    as accepting more work. ``idle`` and ``running`` carry both
    edges so the consumer can render any of "available",
    "busy", "queued" without re-deriving.
    """

    type: Literal["queue_status"]
    idle: bool
    running: bool
    queue_depth: int


class OffloaderQueueStatusChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_QUEUE_STATUS_CHANGED``.

    Fired on the offloader's local bus whenever the
    :class:`PeerLinkClient` receive loop processes an inbound
    ``queue_status`` application frame from a paired receiver.
    The remote-build controller listens, updates its
    RAM-only ``_peer_queue_status`` cache (keyed on
    ``pin_sha256`` since 4a-o part 6), and re-broadcasts via
    the global ``subscribe_events`` stream so frontend clients
    can render per-peer queue depth without polling. Phase-5b
    is the first real application message exercising the 5a
    peer-link foundation end-to-end; the scheduler in phase 7
    reads the same cache to pick the least-busy peer on each
    new offload.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    idle: bool
    running: bool
    queue_depth: int


class OffloaderJobStateChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_JOB_STATE_CHANGED``.

    Fired on the offloader's local bus whenever the
    :class:`PeerLinkClient` receive loop processes an inbound
    ``job_state_changed`` application frame from the receiver
    we submitted *job_id* to. Mirrors
    :class:`JobStateChangedFrameData` plus the source-receiver
    coordinates so downstream subscribers (the controller's
    ``subscribe_events`` re-broadcast, future scheduler hooks)
    can disambiguate transitions across multiple paired
    receivers without parsing the session's identity out of
    a separate cache.

    ``status`` mirrors the wire literal exactly so the
    re-broadcast is byte-for-byte the receiver's frame plus
    the addressing fields. ``error_message`` is empty on
    non-terminal states and on ``completed``; populated on
    ``failed`` / ``cancelled`` with a short human-readable
    string the offloader-side UI can surface.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    error_message: str


class OffloaderJobOutputData(TypedDict):
    r"""
    Payload for ``EventType.OFFLOADER_JOB_OUTPUT``.

    Fired on the offloader's local bus per inbound
    ``job_output`` frame. Mirrors :class:`JobOutputFrameData`
    plus the receiver's coordinates so subscribers can route
    the line to the right peer's output buffer / UI panel.

    ``line`` carries its trailing terminator unchanged
    (``\n`` / ``\r`` / ``\r\n``) — same semantic as the
    receiver-side :class:`JobOutputData` and the wire
    :class:`JobOutputFrameData`; carriage-return-only chunks
    are esptool / PlatformIO progress overwrites, and stripping
    here would lose the signal the renderer leans on to
    decide append-vs-overwrite.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    job_id: str
    stream: Literal["stdout", "stderr"]
    line: str


class OffloaderRemoteBuildsToggledData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_REMOTE_BUILDS_TOGGLED``.

    Fires from :meth:`RemoteBuildController.set_offloader_settings`
    when the operator flips the master "Remote builds enabled"
    switch on the offloader Settings UI (7b). Subscribers are
    the 7b Settings UI on every connected tab — one toggle on
    one tab should flip the switch state on every other open
    tab without a refresh. The scheduler doesn't need this
    event because it reads
    :attr:`RemoteBuildController._remote_builds_enabled` on
    every install via :meth:`build_scheduler_snapshot`; the
    event is purely cross-tab UI sync.
    """

    remote_builds_enabled: bool


class OffloaderPairingEnabledChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIRING_ENABLED_CHANGED``.

    Fires from :meth:`RemoteBuildController.set_pairing_enabled`
    when the operator flips an individual paired-receiver
    enable switch on the offloader Settings UI (7b).
    Subscribers update the matching row's switch state. The
    scheduler reads :attr:`StoredPairing.enabled` directly
    off the in-RAM ``_pairings`` dict via the snapshot, so no
    scheduler-side listener is needed; the event is the
    cross-tab UI sync seam.

    ``pin_sha256`` is the canonical row key (4a-o part 6
    re-keyed offloader state on pin); receivers
    ``(hostname, port)`` aren't on the payload — frontends
    that need them join through their own
    :class:`PairingSummary` snapshot.
    """

    pin_sha256: str
    enabled: bool


class OffloaderRemoteJobSnapshotEntry(TypedDict):
    """
    Snapshot row in the offloader-side in-flight remote-job cache.

    Mirror of :class:`OffloaderJobStateChangedData` minus the
    event-only framing — the receiver's coordinates plus the
    most recent ``status`` / ``error_message`` for an offloader-
    submitted job that hasn't yet reached a terminal state.
    Cached on :attr:`RemoteBuildController._offloader_remote_jobs`
    and surfaced via
    ``subscribe_events.initial_state.remote_jobs`` so a tab
    subscribing AFTER a ``running`` transition still sees the
    job alive without waiting for the next event — same shape
    :class:`PeerQueueStatusSnapshotEntry` uses for queue depth.

    Terminal entries (``completed`` / ``failed`` / ``cancelled``)
    are dropped from the cache on the matching event so the
    snapshot only ever carries in-flight rows. A page reload
    after a build completes shows no entry; the live
    ``OFFLOADER_JOB_STATE_CHANGED`` event the completed
    transition fired is the only signal the frontend got, and
    the frontend keeps its own history if needed.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    error_message: str


class PeerQueueStatusSnapshotEntry(TypedDict):
    """
    Snapshot row in the offloader-side per-peer queue-status cache.

    Mirror of :class:`OffloaderQueueStatusChangedData` minus the
    event-only framing (``type``). Used by
    ``subscribe_events.initial_state.peer_queue_status`` so a
    tab subscribing AFTER an event fired still sees the most
    recent value the offloader observed for each paired peer
    without waiting for the next live event.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    idle: bool
    running: bool
    queue_depth: int


# 5c-1: submit_job + bundle chunking + job lifecycle frames.
# These describe the on-the-wire shape; controller wiring lands
# in 5c-2 (receiver) and 5c-3 (offloader).
class SubmitJobFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB``.

    Header sent by the offloader to announce a build before
    streaming the bundle bytes. Carries the job's identity, the
    target configuration filename (relative to the bundle's
    extracted root, e.g. ``kitchen.yaml``), the build action
    (compile / upload), and the total bundle size + chunk
    count so the receiver can pre-size its assembler and reject
    a mismatched stream cleanly without unbounded buffering.

    ``bundle_sha256`` is the lowercase hex digest of the full
    bundle bytes; the receiver verifies the assembled stream
    against it before accepting the job. Cheap end-to-end
    integrity check on top of the per-frame Noise AEAD;
    catches a chunk-reassembly bug (e.g. a missed
    ``is_last``) that AEAD wouldn't surface.

    ``device_name`` / ``device_friendly_name`` carry the
    offloader's view of the device for the receiver-side
    firmware-tasks UI — the offloader already has both off
    its local Device list at install time, so the receiver
    avoids re-parsing the bundled YAML just to render a
    title. ``NotRequired`` so an older offloader that
    doesn't set them still produces a valid frame; the
    receiver-side title then falls back to the last segment
    of the configuration path. New offloaders always set
    both (empty string for ``device_friendly_name`` when the
    YAML doesn't define one).
    """

    type: Literal["submit_job"]
    job_id: str
    configuration_filename: str
    target: Literal["compile", "upload"]
    total_bundle_bytes: int
    num_chunks: int
    bundle_sha256: str
    device_name: NotRequired[str]
    device_friendly_name: NotRequired[str]


class SubmitJobChunkFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB_CHUNK``.

    One slice of the bundle's gzipped tarball, carrying its
    ordinal index (``chunk_index``) and a flag marking the last
    chunk. Bytes are base64-encoded so the JSON envelope stays
    valid; the receiver decodes back to raw bytes before
    feeding the assembler. Chunks must arrive in monotonic
    order; the assembler rejects out-of-order, duplicate, or
    post-completion frames with a structured error so a
    misbehaving offloader can be ``terminate``'d cleanly
    instead of corrupting the on-disk extract.
    """

    type: Literal["submit_job_chunk"]
    job_id: str
    chunk_index: int
    data_b64: str
    is_last: bool


class SubmitJobAckFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB_ACK``.

    Receiver's response after the bundle stream completes (last
    chunk seen + ``bundle_sha256`` matches). ``accepted`` is
    ``False`` when the job can't be queued; bundle hash
    mismatch, manifest version unsupported, queue full, etc.
    ``reason`` carries the structured error code on rejection
    and is omitted on accept (``NotRequired`` so the wire
    payload is ``{type, job_id, accepted: true}`` on the
    success path with no extra field). The offloader treats a
    missing ack inside :data:`_SUBMIT_JOB_ACK_TIMEOUT_SECONDS`
    as a transport failure and tears the session down; it
    does **not** retry mid-session.
    """

    type: Literal["submit_job_ack"]
    job_id: str
    accepted: bool
    reason: NotRequired[str]


class JobStateChangedFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.JOB_STATE_CHANGED``.

    Receiver-pushed lifecycle transitions for a remote-driven
    job: ``queued`` (post-ack, before the runner picks it up),
    ``running`` (the runner has the slot), ``completed`` /
    ``failed`` / ``cancelled`` (terminal). One frame per
    transition; the firmware controller's existing JOB_*
    events drive the fan-out at the receiver-side wire layer
    in 5c-2.

    ``error_message`` is empty on non-terminal states and on
    ``completed``; populated on ``failed`` / ``cancelled``
    with a short human-readable string the offloader can
    surface to the user. Detailed output (compile errors,
    PlatformIO traces) flows separately through ``job_output``
    so the offloader's UI can render the streaming view
    without parsing the terminal frame.
    """

    type: Literal["job_state_changed"]
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    error_message: str


class JobOutputFrameData(TypedDict):
    r"""
    Application-frame payload for ``AppMessageType.JOB_OUTPUT``.

    Receiver-pushed line of build output. ``stream`` is
    ``stdout`` for the normal compile / upload trace and
    ``stderr`` for warnings / errors; the offloader can
    style them differently when surfacing to the UI without
    re-parsing.

    ``line`` is the raw stdout/stderr text *with its trailing
    terminator preserved* — ``\n``, ``\r``, or ``\r\n``. The
    terminator carries semantic info: carriage-return-only
    chunks are esptool / PlatformIO progress overwrites
    (the offloader's ansi-log renderer leans on the
    distinction to decide whether to append a new line or
    overwrite the last one). Stripping at this layer would
    lose that signal — the receiver-side
    :class:`JobOutputData` bus event preserves terminators
    for the same reason; the wire frame echoes that contract.

    Frames flow at high rate during an active build (one per
    line of compiler / linker output, easily 100+ frames per
    second on a cold compile); the channel's per-frame Noise
    AEAD overhead is the dominant cost. A future optimisation
    can batch consecutive lines into one frame, but 5c-1 keeps
    the wire shape one-line-per-frame for simplicity.
    """

    type: Literal["job_output"]
    job_id: str
    stream: Literal["stdout", "stderr"]
    line: str


class DownloadArtifactsFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.DOWNLOAD_ARTIFACTS``.

    Offloader → receiver request to fetch the build-artifact
    bundle for a previously-completed remote build (phase 6a).
    ``job_id`` is the offloader-supplied id from the original
    ``submit_job`` header — the value the receiver stashed as
    :attr:`FirmwareJob.remote_job_id`. The receiver resolves
    it to the local :class:`FirmwareJob` by walking
    :attr:`FirmwareController._jobs`; the job must be in
    ``COMPLETED`` status (only completed builds have artifacts
    on disk).

    On success the receiver packs the build directory's
    ``.pioenvs/<name>/*.bin`` / ``*.uf2`` outputs plus
    ``idedata.json`` (esphome already emits the latter — it
    carries the per-image flash offsets the offloader's Web
    Serial / esptool path needs) into a gzipped tar, then
    streams back ``artifacts_start`` (header with total_bytes
    + num_chunks + artifacts_sha256) → N ``artifacts_chunk``
    frames → ``artifacts_end{accepted=true}``. On failure
    (unknown correlation, non-terminal job, missing build
    dir, disk read error) the receiver sends
    ``artifacts_end{accepted=false}`` and a structured
    ``reason`` immediately, without any preceding
    ``artifacts_start``.

    The assembled bytes on the offloader side are a tar.gz —
    extracting yields bootloader / partition / firmware
    binaries plus the idedata manifest in one atomic
    transport with a single SHA-256.
    """

    type: Literal["download_artifacts"]
    job_id: str


class ArtifactsStartFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.ARTIFACTS_START``.

    Receiver-pushed header announcing a build-artifact
    tarball stream for the offloader's previously-requested
    ``download_artifacts`` (phase 6a). Carries
    ``total_bytes`` so the offloader can pre-size the
    assembly buffer + reject a mismatched stream cleanly;
    ``num_chunks`` matches the chunk count the receiver will
    actually send (assembler validates against this on every
    chunk); ``artifacts_sha256`` is the lowercase hex digest
    the offloader recomputes after assembly to catch
    chunk-reordering bugs in our own framing (the per-frame
    Noise AEAD already covers wire confidentiality +
    authentication, so the hash isn't a security check).

    ``firmware_offset`` is the lowercase-hex flash offset for
    the ``firmware.bin`` partition (e.g. ``"0x10000"`` on
    ESP32, ``"0x0"`` on ESP8266 / libretiny / RP2040). The
    receiver resolves this once via
    :func:`helpers.build_artifacts._firmware_offset_for_platform`
    against ``StorageJSON.target_platform`` — the offloader
    doesn't have access to that field over the wire and would
    otherwise need to duplicate the platform-detection logic
    upstream esphome already encapsulates. The remaining
    flash-image offsets (bootloader, partitions,
    ota_data_initial) ride inside ``idedata.json`` in the
    tarball, which is the upstream-canonical manifest for
    those entries.

    Fires only on the success path. A failed download sends
    ``artifacts_end`` with ``accepted=false`` and skips
    ``artifacts_start`` entirely.
    """

    type: Literal["artifacts_start"]
    job_id: str
    total_bytes: int
    num_chunks: int
    artifacts_sha256: str
    firmware_offset: str


class ArtifactsChunkFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.ARTIFACTS_CHUNK``.

    One slice of the build-artifact tarball (phase 6a). Same
    wire shape as :class:`SubmitJobChunkFrameData` but for
    the reverse direction — bytes are base64-encoded inside
    the JSON envelope so the dispatch seam stays uniform
    across the bundle-upload and artifacts-download flows.
    The offloader decodes back to raw bytes before feeding
    its :class:`BundleAssembler` (configured with
    :data:`FIRMWARE_MAX_TOTAL_BYTES`). Chunks must arrive in
    monotonic order; the assembler rejects out-of-order,
    duplicate, or post-completion frames with a structured
    error so a misbehaving receiver can be ``terminate``'d
    cleanly instead of corrupting the assembled bytes.
    """

    type: Literal["artifacts_chunk"]
    job_id: str
    chunk_index: int
    data_b64: str
    is_last: bool


class ArtifactsEndFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.ARTIFACTS_END``.

    Receiver's terminator frame for a ``download_artifacts``
    request (phase 6a). Doubles as the success/failure ack:

    * **Success path** — fires after the last chunk
      (``is_last=true``) has been sent; ``accepted=true``,
      ``reason`` omitted. The offloader validates the
      assembled bytes against the announced
      ``artifacts_sha256`` from ``artifacts_start`` before
      resolving the per-job download future with the
      tarball bytes.
    * **Failure path** — fires *instead of* any
      ``artifacts_start`` / ``artifacts_chunk`` when the
      receiver-side dispatch refuses the request upfront
      (unknown correlation, non-terminal job, missing build
      dir, pack failure, disk error). ``accepted=false``
      with a structured ``reason``; ``reason`` is omitted
      on accept (``NotRequired`` so the success payload is
      ``{type, job_id, accepted: true}`` with no extra
      field).
    """

    type: Literal["artifacts_end"]
    job_id: str
    accepted: bool
    reason: NotRequired[str]


class CancelJobFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.CANCEL_JOB``.

    Offloader → receiver cooperative cancel for a previously-
    submitted job (phase 5d). ``job_id`` is the
    offloader-supplied id from the original ``submit_job``
    header — i.e. the value the offloader generated and the
    receiver stashed as :attr:`FirmwareJob.remote_job_id`. The
    receiver resolves the offloader-side id back to its local
    :class:`FirmwareJob` via the :class:`JobFanout` correlation
    cache (keyed on ``(remote_peer=session.dashboard_id,
    remote_job_id)``) and routes the cancel through the
    firmware queue's existing :meth:`FirmwareController.cancel`
    primitive.

    No ack frame in the reverse direction: cancellation is
    fire-and-forget. The receiver's next ``job_state_changed``
    with ``status="cancelled"`` is the confirmation the
    offloader already plumbs through
    :attr:`EventType.OFFLOADER_JOB_STATE_CHANGED`. A
    cancel-of-already-terminal job raises
    :class:`CommandError(INVALID_ARGS)` inside
    :meth:`FirmwareController.cancel` which the handler
    swallows + debug-logs — the receiver was about to (or
    already has) emitted the natural terminal event and no
    further wire activity is needed. A cancel-of-unknown-job
    is debug-logged at the receiver and dropped (typically a
    race between offloader send and receiver-side terminal
    transition that already evicted the
    :class:`JobFanout` correlation entry).
    """

    type: Literal["cancel_job"]
    job_id: str


@dataclass
class StoredPeer(DataClassORJSONMixin):
    """
    Receiver-side record of a paired (or pending) offloader.

    Persisted under ``_remote_build.peers``. Created by the
    pair-request flow over the peer-link WS: an offloader runs
    a Noise XX handshake with ``intent="pair_request"`` and a
    payload carrying its ``label`` + ``dashboard_id``. The
    receiver reads the offloader's static X25519 pubkey from
    the Noise handshake itself (no cert involved) and stores it.

    ``static_x25519_pub`` is the canonical identifier the
    Noise handshake binds to. ``pin_sha256`` is its lowercase-
    hex SHA-256, used for log lines / event payloads / wire
    fields where we already have a hex-pin convention.

    ``dashboard_id`` is the offloader's stable identity from
    phase 3a; sent in the pair_request payload so the admin UI
    has a friendly identifier (the X25519 pubkey alone doesn't
    carry one). Primary key for the receiver WS surface
    (``approve_peer({dashboard_id})`` etc.) so a future X25519
    keypair rotation on the offloader's side doesn't change the
    user-facing handle.

    ``label`` is a human-readable name the offloader's user
    sets during pair (e.g. ``green``, ``laptop``).

    ``peer_ip`` is the source IP we observed the offloader's
    pair_request handshake from. Persisted (rather than carried
    only on the live event) so the receiver Settings inbox can
    surface it for clone-risk sanity-check on rows that landed
    before the admin opened the page. Empty string when unknown
    — legacy rows from receivers that pre-date this field load
    cleanly with an empty default and the frontend hides the IP
    line when blank.
    """

    dashboard_id: str
    pin_sha256: str
    static_x25519_pub: bytes
    label: str
    paired_at: float
    peer_ip: str = ""

    def refresh_from_pair_request(
        self,
        *,
        pin_sha256: str,
        static_x25519_pub: bytes,
        label: str,
        paired_at: float,
        peer_ip: str,
    ) -> None:
        """
        Update the fields a fresh ``intent="pair_request"`` supplies.

        Owns the contract for "what changes on re-pair": the X25519
        pubkey + its hash (offloader rotated their identity), the
        label (renamed dashboard), the ``paired_at`` timestamp (so
        the receiver-side inbox sorts the most-recent attempt
        first), and the source ``peer_ip`` (offloader could have
        moved interfaces / DHCP renewed). ``dashboard_id`` is the
        row's primary key and is intentionally left out of the
        refresh set; ``status`` is also left out because
        pair_request never changes status by itself (the
        receiver-side user's Accept / Reject does, via
        ``approve_peer`` / ``remove_peer``).

        Caller is responsible for the no-demote-when-APPROVED
        check before invoking this; calling
        ``refresh_from_pair_request`` on an APPROVED row would
        silently overwrite the originally-pinned pubkey, which is
        the wrong outcome (see ``record_pair_request`` for the
        gating logic).
        """
        self.pin_sha256 = pin_sha256
        self.static_x25519_pub = static_x25519_pub
        self.label = label
        self.paired_at = paired_at
        self.peer_ip = peer_ip


@dataclass
class PeerSummary(DataClassORJSONMixin):
    """
    Public-facing wire view of :class:`StoredPeer`.

    Drops ``static_x25519_pub`` — the raw 32-byte pubkey is
    on-disk only; ``pin_sha256`` (lowercase-hex SHA-256 of the
    pubkey) is the wire-friendly form that UIs render for
    OOB-verification. ``peer_ip`` is the source IP observed at
    pair_request time; the receiver Settings inbox renders it
    next to the pin so the operator has a second sanity-check
    against a clone scenario (an attacker on a different IP
    submitting a pair_request with a spoofed label or against a
    drifted dashboard_id). Empty string for legacy rows from
    receivers that pre-date the persisted ``peer_ip`` field.

    ``connected`` reports whether the receiver currently has
    an active 5a-2 peer-link session for this peer
    (``dashboard_id`` membership in
    :attr:`RemoteBuildController._peer_link_sessions`). The
    field is computed at snapshot-build time from the
    receiver's RAM-canonical session registry — not stored
    on disk — and live updates flow through the
    :attr:`EventType.RECEIVER_PEER_LINK_SESSION_OPENED` /
    ``_CLOSED`` bus events so a tab subscribing AFTER an
    open / close still sees current state from the snapshot.
    Always ``False`` for PENDING peers: peer-link is gated on
    APPROVED status (the receiver's
    :meth:`RemoteBuildController.lookup_peer_for_session`
    only returns ``OK`` for APPROVED rows), so a PENDING peer
    can never have a registered session.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    paired_at: float
    status: PeerStatus
    peer_ip: str = ""
    connected: bool = False


# Bounds enforced both at the WS-command boundary (the future
# 4a-o ``request_pair`` validators) and at the storage seam
# (``StoredPairing.__post_init__``). Defense-in-depth for the
# storage layer keeps a hand-edited sidecar from smuggling huge
# values past the WS validators — the on-disk shape is the same
# trust surface as anything else under ``<config_dir>``, and a
# malformed row is the kind of thing the loader's
# ``DataClassORJSONMixin.from_dict`` happily round-trips
# regardless of size.
#
# Schema is voluptuous because (a) it's already a transitive
# dep through ESPHome's ``config_validation``, (b) declarative
# field bounds beat hand-rolled if-chains for readability, and
# (c) the upstream ESPHome codebase uses the same primitive so
# contributors moving between repos see a familiar shape.
#
# **Maintenance note.** The schema below is a second source of
# truth alongside the dataclass field annotations: each
# ``vol.Required(...)`` mirrors a field declared on
# :class:`StoredPairing`, and dropping or renaming a field in
# one place without the other will silently desync (the schema
# would either fail-pass — ``vol.Required`` against a missing
# key raises — or accept-everything if the annotation widens).
# Keep the two in lockstep when changing the row shape: add /
# rename / remove the dataclass field AND the corresponding
# schema entry in the same change.
#
# **No comparable schema on :class:`StoredPeer`.** Both rows
# have a disk-reconstruction path (``_decode_pairings`` /
# ``_decode_peers`` call ``from_dict`` on controller start),
# so the validator gap on :class:`StoredPeer` is real — a
# range-violating value (negative port, non-hex pin, etc.)
# could survive into runtime if it landed on disk. We accept
# the gap because:
#
# 1. The fail-closed-on-corruption posture both ``_decode_*``
#    functions take catches the load-bearing failure mode.
#    A malformed JSON blob or a type mismatch mashumaro can't
#    coerce raises out of ``from_dict``; the outer
#    ``except Exception`` resets the store to empty rather
#    than crashing dashboard startup. ``StoredPeer``'s
#    dataclass type annotations get most of that for free
#    via mashumaro.
# 2. The receiver-side row is constructively narrow: every
#    field a schema would validate is gated upstream at
#    *write* time (Noise XX pins ``static_x25519_pub`` at
#    32 bytes; the dispatcher validates ``dashboard_id``
#    against ``DASHBOARD_ID_PATTERN``; ``_normalize_label``
#    caps ``label`` before ``record_pair_request`` is
#    called). A corrupt disk row would have to come from
#    *us* writing bad data, not from a malicious peer.
# 3. The defense-in-depth a schema would add (catching
#    logically-invalid-but-type-compatible disk corruption
#    on data we wrote) is lower-value than the maintenance
#    cost of a second source of truth alongside the
#    dataclass annotations.
#
# Cap on :attr:`StoredPairing.esphome_version`. The validator
# below rejects rows whose stored version exceeds this length;
# the wire-extract path on the offloader side
# (:func:`controllers.remote_build.peer_link_client._extract_receiver_esphome_version`)
# applies the same cap before writing the field so a malicious
# / buggy receiver can't poison the sidecar with a multi-MB
# string that then fails to load on the next start. 64 chars is
# generous for any real ``esphome.const.__version__``
# (``"2026.5.0-dev"`` is 13 chars) — the cap is the "this isn't
# a version string anymore" boundary, not a tight fit.
PAIRING_VERSION_MAX_LEN = 64

# :class:`StoredPairing` carries a schema because its
# offloader-side write path is broader (user-controlled
# ``request_pair`` args reach the controller through fewer
# upstream gates than the receiver-side equivalents) and a
# fail-closed disk shape is more valuable there. The
# asymmetry is the honest reflection of the asymmetric
# write-side trust, not an outstanding bug.
_PAIRING_VALIDATOR = vol.Schema(
    {
        # RFC 1035 §2.3.4 caps a fully-qualified domain name at 253
        # characters; round up to 255 to leave room for trailing-dot
        # variations. The ``\S`` requirement rejects whitespace-only
        # values that would otherwise pass ``Length(min=1)`` —
        # storing a hostname that can't resolve is worse than
        # rejecting at write time. Normalisation (strip + lowercase)
        # is the WS-command validator's job; the storage seam just
        # rejects malformed rows.
        vol.Required("receiver_hostname"): vol.All(
            str, vol.Length(min=1, max=255), vol.Match(r"\S")
        ),
        # :func:`not_bool` first because voluptuous's ``int`` check
        # accepts ``bool`` (Python's ``isinstance(True, int)`` is true) —
        # without the explicit reject, ``receiver_port=True`` would pass
        # as port 1.
        vol.Required("receiver_port"): vol.All(not_bool, int, vol.Range(min=1, max=65535)),
        # Lowercase-hex SHA-256: 64 chars from ``[0-9a-f]``. Factory
        # in ``helpers/voluptuous_validators.py`` so the same shape
        # can be reused by the 4a-o WS-command validators without
        # drifting.
        vol.Required("pin_sha256"): lowercase_hex(64),
        # ``static_x25519_pub`` is the raw X25519 pubkey — exactly 32
        # bytes per RFC 7748 §5.
        vol.Required("static_x25519_pub"): vol.All(bytes, vol.Length(min=32, max=32)),
        vol.Required("label"): vol.All(str, vol.Length(max=128)),
        # ``vol.All(not_bool, ...)`` rather than ``vol.Any(int, float,
        # not_bool)`` because ``Any`` short-circuits on the first
        # accepting branch — ``int`` would accept ``True``
        # (``isinstance(True, int)`` is true) before ever reaching the
        # bool reject. Run ``not_bool`` first, then assert int-or-float.
        vol.Required("paired_at"): vol.All(not_bool, vol.Any(int, float)),
        # ``status`` is in-memory only (the controller's serialiser
        # filters PENDING rows out before writing to disk so the
        # on-disk shape stays APPROVED-only), but ``__post_init__``
        # runs the validator over ``asdict(self)`` which includes
        # the field, so the schema must accept it. ``vol.In(PeerStatus)``
        # matches both the enum instance (live constructor paths)
        # and the bare string forms (``"pending"`` / ``"approved"``)
        # that ``DataClassORJSONMixin.from_dict`` produces when
        # round-tripping through JSON.
        vol.Required("status"): vol.In(PeerStatus),
        # Receiver's ``esphome.const.__version__`` captured at
        # peer-link session-open time. In-RAM only at session
        # close; persisted across restarts so a disconnected row
        # still carries its last-seen version for the UI's
        # "last known: X.Y.Z" display. Capped to keep a corrupt
        # / hand-edited sidecar from landing a megabyte string.
        # Empty when no peer-link session has opened yet (fresh
        # PENDING row, or an old sidecar from before this field
        # existed); pick_build_path's version-compat gate
        # accepts empty as "unknown, fall through to compat".
        vol.Required("esphome_version"): vol.All(str, vol.Length(max=PAIRING_VERSION_MAX_LEN)),
        # Per-pairing master toggle (7b). The 7b Settings UI
        # exposes one switch per paired build server; when
        # ``False`` the scheduler skips this row entirely (the
        # operator wants this receiver paired but doesn't want
        # transparent install to route here). ``not_bool`` not
        # needed — ``bool`` is the strict accept, and an
        # explicit ``bool`` requirement rejects the same
        # ``int``-coerced-to-bool shape that bit the
        # cleanup_ttl_seconds validator.
        vol.Required("enabled"): bool,
    }
)


@dataclass
class StoredPairing(DataClassORJSONMixin):
    """
    Offloader-side record of a paired (or pending) receiver.

    Persisted in the per-file
    :class:`~helpers.storage.Store` at
    ``<config_dir>/.offloader_pairings.json`` (RAM-first model:
    the controller's ``_pairings`` dict is the runtime source of
    truth, and the ``Store`` debounce-saves APPROVED rows to
    disk). Created by the ``request_pair`` flow over the
    peer-link WS: the offloader runs a Noise XX handshake with
    ``intent="pair_request"``, captures the receiver's static
    X25519 pubkey from the handshake transcript, and stores it
    here together with the receiver's ``(hostname, port)``
    coordinates.

    This is the offloader-side counterpart to
    :class:`StoredPeer` (receiver-side). The two shapes are
    deliberately *not* the same row passed both ways: the
    receiver's ``StoredPeer`` keys on the offloader's
    ``dashboard_id`` (the offloader's stable identity), while
    the offloader's ``StoredPairing`` keys on
    ``(receiver_hostname, receiver_port)`` because that's what
    the user enters in the Pair dialog and what reconnection
    needs.

    ``static_x25519_pub`` is the canonical identifier the Noise
    handshake binds to. ``pin_sha256`` is its lowercase-hex
    SHA-256 (the OOB-verified pin the user confirmed on the
    receiver's Build server card). Both are stored so a future
    re-pair can detect a receiver-side identity rotation
    (handshake's pubkey-hash drifts from the stored
    ``pin_sha256``) without re-deriving from the raw bytes on
    every connect.

    ``label`` is the human-readable name the offloader's user
    typed when pairing (e.g. ``desktop``, ``build server``).
    Surfaced in the offloader's settings list; not sent to the
    receiver — the receiver gets its own ``label`` from the
    offloader's pair_request payload (the offloader-supplied
    name FOR the offloader, not for the receiver).

    ``status`` is the row's lifecycle position. The controller
    holds one in-RAM dict containing both PENDING and APPROVED
    rows; the disk filter in ``_serialize_pairings`` strips
    PENDING rows so the on-disk shape stays APPROVED-only.
    ``status`` defaults to ``APPROVED`` for two reasons:

    * **Disk shape invariant** — only APPROVED rows ever reach
      disk, so reading a row back from the per-file ``Store``
      always produces an APPROVED row. The default matches the
      invariant.
    * **Test ergonomics** — fixtures and ad-hoc constructions
      that don't care about lifecycle (most of the
      validator-shape tests, the reflection-driven event-payload
      contracts) don't have to thread a status arg through.

    PENDING is the explicit case — ``request_pair`` sets it on
    the row before adding it to the dict; ``_apply_pair_status_result``
    flips it to APPROVED in place when the receiver reports the
    flip.

    ``__post_init__`` enforces upper bounds on the user-supplied
    string fields (hostname, label) + shape on the cryptographic
    fields so a malformed sidecar row (hand-edit, partial-write
    recovery, schema-skew across ESPHome upgrades) is rejected
    by the loader's ``from_dict`` rather than landing as a
    multi-megabyte string in memory + on the wire.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    static_x25519_pub: bytes
    label: str
    paired_at: float
    status: PeerStatus = PeerStatus.APPROVED
    # Receiver-advertised ``esphome.const.__version__``, captured
    # on every peer-link session-open from the ``intent_response``
    # post-handshake payload. Empty on a fresh PENDING row + on
    # APPROVED rows loaded from an older sidecar that predates
    # this field (``DataClassORJSONMixin.from_dict`` tolerates
    # missing fields with declared dataclass defaults; the empty
    # string default below is what fills in). The version
    # refreshes on every reconnect — pick_build_path consumes
    # the in-RAM value so a receiver upgrade picks up on the
    # next session-open without operator action; the persisted
    # copy is the cross-restart fallback for "last known
    # version" UI display.
    esphome_version: str = ""
    # 7b per-pairing master toggle. ``True`` matches the
    # historical implicit behaviour (every APPROVED row is
    # eligible for the scheduler), so older sidecars from
    # before this field landed deserialise as enabled.
    # When ``False`` the scheduler's
    # :func:`helpers.build_scheduler.pick_build_path` skips
    # this row — the operator wants the receiver paired
    # (peer-link stays open, Send-builds power-user surface
    # still works) but doesn't want transparent install to
    # route here.
    enabled: bool = True

    def __post_init__(self) -> None:
        """Run :data:`_PAIRING_VALIDATOR`; re-raise as ``ValueError``."""
        try:
            _PAIRING_VALIDATOR(asdict(self))
        except vol.Invalid as exc:
            field_name = exc.path[0] if exc.path else "<row>"
            raise ValueError(f"StoredPairing.{field_name}: {exc.msg}") from exc


@dataclass
class PairingSummary(DataClassORJSONMixin):
    """
    Public-facing wire view of :class:`StoredPairing`.

    Drops ``static_x25519_pub`` — the raw 32-byte pubkey is
    on-disk only; ``pin_sha256`` is the wire-friendly form
    UIs render. Mirrors the :class:`PeerSummary` projection
    seam on the receiver side so a future "store extra
    offloader-only fields on the row" change can't leak those
    by accident.

    ``connected`` is the offloader-side mirror of
    :attr:`PeerSummary.connected`: it reports whether the
    offloader's per-pairing :class:`PeerLinkClient` task
    currently has an open peer-link session against the
    receiver. Computed at snapshot-build time from
    :attr:`RemoteBuildController._open_peer_links`
    (a ``set[str]`` of ``pin_sha256`` values, populated by
    listeners on :attr:`EventType.OFFLOADER_PEER_LINK_OPENED` /
    :attr:`EventType.OFFLOADER_PEER_LINK_CLOSED` that
    :class:`PeerLinkClient` already fires from
    :meth:`_fire_opened` / :meth:`_fire_closed`). Always
    ``False`` for PENDING rows — the offloader doesn't spawn
    a peer-link client until the receiver flips the row to
    APPROVED.

    ``connecting`` is the "live but not connected" state: the
    per-pairing client task is alive (not orphaned) and not
    currently sitting on an open session. The reconnect-backoff
    loop in :meth:`PeerLinkClient.run` cycles through this
    state every time a session ends and the client is about to
    retry. UI uses it to render a "Connecting…" indicator
    distinct from a permanently-orphaned pairing (pin mismatch,
    superseded) where ``connecting`` is ``False`` *and*
    ``connected`` is ``False`` — the operator's recovery there
    is re-pair / unpair, not "wait for reconnect."

    ``last_connect_error`` is a one-line human-readable
    description of the most recent connection failure
    (``"<ExceptionType>: <message>"`` form for transport /
    Noise errors, ``"auth rejected"`` for handshake-rejected
    sessions, ``"pin mismatch"`` for the orphan-on-rotation
    path). Cleared when a session reaches the post-handshake
    open state. Empty on a never-connected pairing where the
    client task hasn't completed its first attempt yet.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    label: str
    paired_at: float
    status: PeerStatus
    connected: bool = False
    connecting: bool = False
    last_connect_error: str = ""
    # Receiver-advertised ``esphome.const.__version__``, mirroring
    # :attr:`StoredPairing.esphome_version`. Refreshed on every
    # peer-link session-open; empty on a never-connected row /
    # an older sidecar. Surfaced in the offloader's paired-
    # receivers UI as a "last known: X.Y.Z" line so the operator
    # can spot a version skew before the version-compat gate in
    # pick_build_path silent-fallbacks them to LOCAL.
    esphome_version: str = ""
    # 7b per-pairing enable toggle, mirroring
    # :attr:`StoredPairing.enabled`. The 7b Settings UI
    # renders the switch from this field.
    enabled: bool = True


# Default + bounds for :attr:`RemoteBuildSettings.cleanup_ttl_seconds`.
# 24h matches 6c's "subtree is cold if it hasn't been
# submitted-to for a day" intuition. Bounds: 1h floor keeps the
# operator from setting "delete everything every sweep tick" by
# accident; 30d ceiling keeps the cap somewhere finite for the
# input validator (the disk-walk doesn't care about the upper
# bound, but a typed cap surfaces silly inputs at the WS layer
# rather than landing them on disk).
DEFAULT_CLEANUP_TTL_SECONDS = 24 * 60 * 60
MIN_CLEANUP_TTL_SECONDS = 60 * 60
MAX_CLEANUP_TTL_SECONDS = 30 * 24 * 60 * 60


@dataclass
class RemoteBuildSettings(DataClassORJSONMixin):
    """
    Receiver-side settings for the remote-build feature (storage shape).

    Stored in ``.device-builder.json`` under the ``_remote_build``
    top-level key. Carries the master ``enabled`` toggle and the
    6c TTL sweep's ``cleanup_ttl_seconds`` knob. APPROVED
    :class:`StoredPeer` rows live in their own per-file
    :class:`~helpers.storage.Store` at
    ``<config_dir>/.receiver_peers.json`` (mirrors the offloader-
    side :class:`OffloaderRemoteBuildSettings` shape) so reads
    short-circuit through RAM and don't race a write in flight.
    Legacy ``peers`` and ``manual_hosts`` entries on older
    sidecars are silently ignored at load time — the
    ``manual_hosts`` flow was removed once the pair dialog
    started typing hostnames straight into ``request_pair``, and
    the ``tokens`` list (hash-only bearer tokens) went with the
    dormant bearer machinery in phase 4a-r2.

    ``enabled`` is the master gate the dashboard checks before
    binding the receiver site. Defaults to ``True`` so fresh
    installs are LAN-discoverable and pairable without an
    extra operator step — the feature's discoverability is the
    point, and the actual privilege grant happens at the
    receiver-side **pair-approval dialog** (a paired peer can
    submit jobs, but pairing requires explicit user approval
    on this dashboard, so binding the port alone grants
    nothing).

    HA-addon deployments override this default at the bind
    site: a fresh addon install with no persisted
    ``_remote_build`` block in metadata does not bind. The
    addon's docker container doesn't expose port 6055 to the
    LAN by default, so binding it would waste the port and
    confuse operators. HA-addon operators who DO want the
    feature (e.g. they've added ``6055/tcp`` to the addon's
    ``ports:`` config, as some legacy-dashboard operators have
    historically done with the HTTP port) flip the toggle in
    Settings; the resulting write persists the block, the
    "explicit operator opt-in" signal flips, and the next
    boot's bind site respects the persisted ``enabled`` field
    exactly like every other deployment mode. See
    :meth:`device_builder.DeviceBuilder._maybe_start_remote_build_site`
    for the gate.

    ``cleanup_ttl_seconds`` is the operator-tunable threshold
    the 6c background sweep uses to decide a remote-build
    subtree is cold enough to delete. Defaults to 24h
    (:data:`DEFAULT_CLEANUP_TTL_SECONDS`); the WS validator
    caps the input between :data:`MIN_CLEANUP_TTL_SECONDS` and
    :data:`MAX_CLEANUP_TTL_SECONDS` so a fat-fingered or
    malicious write can't push the sweep to "delete everything
    every tick" or "never reclaim disk". Missing on an older
    sidecar deserialises to the default via the dataclass
    field default.
    """

    enabled: bool = True
    cleanup_ttl_seconds: int = DEFAULT_CLEANUP_TTL_SECONDS

    def __post_init__(self) -> None:
        """Coerce + clamp ``cleanup_ttl_seconds`` on load.

        The WS validator on :meth:`RemoteBuildController.set_settings`
        gates writes that come through the WS surface, but the
        on-disk decode path (``from_dict`` →
        ``RemoteBuildSettings(...)``) doesn't apply the same
        ``not_bool`` / range check. A hand-edited or corrupt
        sidecar with ``cleanup_ttl_seconds: true`` would
        deserialise as ``1`` (bool is an int subclass), and
        the sweep would treat anything older than 1s as cold —
        near-immediate cache deletion. Other wrong types (string,
        float, None) would propagate to the sweep's ``now -
        ttl_seconds`` arithmetic and raise ``TypeError``, which
        the controller's cleanup loop catches but logs every
        cycle.

        Both failure modes resolve to the safe default: coerce
        non-int / bool values back to
        :data:`DEFAULT_CLEANUP_TTL_SECONDS` and clamp the
        result to [:data:`MIN_CLEANUP_TTL_SECONDS`,
        :data:`MAX_CLEANUP_TTL_SECONDS`]. The ``enabled``
        toggle is left alone — a bad cleanup TTL shouldn't
        flip the master switch.

        Doesn't reject the row (no ``ValueError``) so the
        load path stays robust against partially-corrupt
        sidecars; the operator's last good ``enabled`` value
        survives even if the TTL field is broken.
        """
        if isinstance(self.cleanup_ttl_seconds, bool) or not isinstance(
            self.cleanup_ttl_seconds, int
        ):
            self.cleanup_ttl_seconds = DEFAULT_CLEANUP_TTL_SECONDS
            return
        if self.cleanup_ttl_seconds < MIN_CLEANUP_TTL_SECONDS:
            self.cleanup_ttl_seconds = MIN_CLEANUP_TTL_SECONDS
        elif self.cleanup_ttl_seconds > MAX_CLEANUP_TTL_SECONDS:
            self.cleanup_ttl_seconds = MAX_CLEANUP_TTL_SECONDS


@dataclass
class RemoteBuildSettingsView(DataClassORJSONMixin):
    """
    Wire view of :class:`RemoteBuildSettings`.

    Returned from every WS command that exposes settings to a
    client. Mirrors :class:`RemoteBuildSettings` plus the
    ``peers`` projection (PENDING + APPROVED merged from the
    controller's in-memory dicts and projected to
    :class:`PeerSummary` so the raw X25519 pubkey bytes never
    reach the wire). The frontend's primary peer surface is the
    ``subscribe_events`` initial-state push + bus events; the
    field is kept here so a client that round-trips
    :meth:`set_settings` / :meth:`get_settings` sees a
    consistent shape with what the snapshot delivered.
    """

    enabled: bool = True
    cleanup_ttl_seconds: int = DEFAULT_CLEANUP_TTL_SECONDS
    peers: list[PeerSummary] = field(default_factory=list)


@dataclass
class ReceiverPeers(DataClassORJSONMixin):
    """
    Receiver-side APPROVED peers (storage shape).

    Stored in its own per-file :class:`~helpers.storage.Store`
    instance at ``<config_dir>/.receiver_peers.json`` — sibling
    of the metadata sidecar rather than a sub-key of it, so
    atomic writes are per-domain (corrupting the peers file
    can't take out the rest of ``.device-builder.json``) and a
    receiver-only mutation doesn't have to acquire the metadata
    transaction lock. Mirrors the offloader-side
    :class:`OffloaderRemoteBuildSettings` shape exactly: one
    ``StoredPeer`` list, no other fields, RAM-canonical at
    runtime.

    PENDING peers live in ``RemoteBuildController._pending_peers``
    and are never persisted (their lifetime is bounded by the
    pairing window). Only APPROVED rows reach this file.
    """

    peers: list[StoredPeer] = field(default_factory=list)


@dataclass
class OffloaderRemoteBuildSettings(DataClassORJSONMixin):
    """
    Offloader-side settings for the remote-build feature (storage shape).

    Stored in its own per-file :class:`~helpers.storage.Store`
    instance at ``<config_dir>/.offloader_pairings.json`` —
    sibling of the metadata sidecar rather than a sub-key of it,
    so atomic writes are per-domain (corrupting the offloader
    pairings file can't take out the receiver-side
    ``.device-builder.json``) and there's no lock contention
    against unrelated metadata writers. A dashboard playing both
    roles persists each side's state independently; a future
    "split offloader / receiver into separate processes" refactor
    only has to move one file.

    ``pairings`` carries phase-4a-o :class:`StoredPairing`
    rows: the offloader's pinned receivers.

    ``remote_builds_enabled`` (7b) is the master switch the
    scheduler reads. When ``False`` the transparent install
    flow short-circuits to LOCAL for every device — paired
    receivers stay paired and the peer-link sessions stay
    open (the explicit Send-builds power-user dialog still
    works); only the implicit "Install → maybe route to a
    receiver" path is gated off. Default ``True`` matches
    the implicit behaviour the dashboard had before this
    field existed: any APPROVED + connected + idle pairing
    was eligible. Older sidecars from before the field
    landed deserialise as enabled.
    """

    pairings: list[StoredPairing] = field(default_factory=list)
    remote_builds_enabled: bool = True


@dataclass
class OffloaderRemoteBuildSettingsView(DataClassORJSONMixin):
    """
    Wire view of :class:`OffloaderRemoteBuildSettings`.

    Returned from offloader-side WS commands. ``pairings`` is
    projected to :class:`PairingSummary` (drops
    ``static_x25519_pub``); same projection-seam pattern as
    :class:`RemoteBuildSettingsView`.

    ``remote_builds_enabled`` (7b) mirrors the storage-shape
    master toggle so the frontend's "Remote builds enabled"
    switch can read its initial state from the same
    ``get_offloader_settings`` round-trip that already
    surfaces the pairings list.
    """

    pairings: list[PairingSummary] = field(default_factory=list)
    remote_builds_enabled: bool = True


@dataclass
class PairingWindowState(DataClassORJSONMixin):
    """
    In-process pairing-window state on the receiver.

    The pairing window narrows when ``intent="pair_request"``
    Noise frames are even accepted: only while the receiver-side
    Pairing requests screen is mounted. ``open`` is the boolean
    state; ``expires_in_seconds`` is the remaining lifetime when
    the window is open (``None`` when closed). The frontend
    renders a live countdown from ``expires_in_seconds`` and
    re-extends by calling ``remote_build/set_pairing_window``
    with ``open=true`` on each activity-driven extend tick (one
    call per 30s on the wire).

    Wire shape for the ``set_pairing_window`` response and the
    ``remote_build_pairing_window_changed`` event payload. Not
    persisted; the per-client extend timestamps live in
    :attr:`RemoteBuildController._pairing_window_clients` and the
    auto-close timer in
    :attr:`RemoteBuildController._pairing_window_handle`. State
    resets on every dashboard restart (which is fine; the
    receiving dashboard's user re-opens the Pairing requests
    screen after restart and the window opens fresh).
    """

    open: bool
    expires_in_seconds: float | None = None


@dataclass
class RemoteBuildPeer(DataClassORJSONMixin):
    """
    A peer dashboard known to this dashboard.

    Wire shape returned from ``remote_build/list_hosts``. Two
    sources land in the same row shape:

    * ``source=MDNS``: discovered via the
      ``_esphomebuilder._tcp.local.`` browse. ``name`` is the
      mDNS service-instance name (leftmost label, e.g.
      ``desktop``); ``hostname`` is the SRV target (e.g.
      ``desktop.local.``); ``addresses`` is the parsed A / AAAA
      list with IPv6 scope preserved; versions come from TXT.
    * ``source=MANUAL``: user-supplied via
      ``remote_build/add_manual_host``. ``name`` is the full
      hostname verbatim (NOT the leftmost label) so an IP-only
      entry like ``192.168.1.10`` reads sensibly in the UI rather
      than truncating to ``"192"``. ``hostname`` is the same
      user-entered string, ``port`` is the user-entered port,
      ``addresses`` is empty, and version fields are blank until
      phase 4 attempts the connection.

    Phase 2 stops at discovery + manual entry; pairing / connection
    / fingerprint pinning lands in later phases.
    """

    name: str
    hostname: str
    port: int
    source: RemoteBuildPeerSource
    addresses: list[str] = field(default_factory=list)
    server_version: str = ""
    esphome_version: str = ""
    # Receiver's peer-link X25519 static pubkey hash (lowercase-
    # hex SHA-256, the same value as
    # :attr:`StoredPairing.pin_sha256`) and peer-link Noise WS
    # port, both pulled out of the
    # ``_esphomebuilder._tcp.local.`` TXT record. Distinct from
    # the dashboard's 3a TLS cert SPKI fingerprint; the
    # peer-link identity is its own X25519 keypair (see
    # ``helpers/peer_link_identity.py``). The offloader uses
    # both to match a discovered broadcast against a stored
    # pairing's ``pin_sha256`` and dial the right peer-link
    # port for the auto-rebind probe (4a-o part 7). Empty
    # string / 0 for: receivers that haven't bound the
    # peer-link listener (default-off mode), and ``MANUAL``
    # rows (which never go through the mDNS resolve path; the
    # user typed the hostname/port and the pair flow captures
    # the pin into ``StoredPairing`` rather than back onto this
    # row).
    pin_sha256: str = ""
    remote_build_port: int = 0


@dataclass
class IdentityView(DataClassORJSONMixin):
    """
    Receiver-side dashboard identity, projected for the Settings UI.

    Returned from ``remote_build/get_identity`` and
    ``remote_build/rotate_identity``. The X25519 private key
    is intentionally NOT included: only the ``pin_sha256`` (the
    SHA-256 of the X25519 public key, lowercase hex) is safe to
    ship, and the pubkey itself adds nothing the fingerprint
    doesn't already let an offloader pin against.

    ``server_version`` is this dashboard's package version;
    ``esphome_version`` is the bundled esphome's. Both are also
    advertised in mDNS TXT (see :class:`DashboardAdvertiser`),
    but the Settings UI doesn't browse mDNS to render its own
    "Build host" card — surfacing them here keeps the card a
    single WS call.

    ``listener_bound`` reports whether the
    peer-link Noise WS listener is currently
    serving traffic on this dashboard. Lets the Settings UI
    distinguish "rotation succeeded AND the listener is back
    up" from "rotation succeeded but the rebuild fail-softed"
    (port now bound by something else, cert load throws, …).
    The latter is silent in the logs without this flag.
    """

    dashboard_id: str
    pin_sha256: str
    server_version: str
    esphome_version: str
    listener_bound: bool = False
