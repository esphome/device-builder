"""Offloader-side TypedDict event payloads + alert / snapshot wire shapes."""

from __future__ import annotations

from typing import Literal, TypedDict

# ---------------------------------------------------------------------------
# Event payload shapes (TypedDict so the bus.fire data dict is type-checked at
# the call site without changing the wire shape; mirrors HA's
# ``EventStateChangedData`` / ``EventStateReportedData`` pattern).
# ---------------------------------------------------------------------------


class OffloaderPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_STATUS_CHANGED``.

    Offloader-side counterpart to
    :class:`RemoteBuildPairStatusChangedData`. Fired on the
    offloader's local bus from two paths:

    * The per-row pair-status listener task
      (``OffloaderController._await_pair_status_flip`` →
      ``_apply_pair_status_result`` → ``_fire_offloader_pair_status_changed``)
      when a previously-PENDING :class:`StoredPairing` flips to
      ``APPROVED`` (admin clicked Accept) or is dropped because
      the receiver returned ``REJECTED`` (admin clicked Reject;
      window closed clearing the receiver-side dict; row never
      existed; pin rotated).
    * ``OffloaderController.unpair`` when the user removes a
      row, so other clients on the global ``subscribe_events``
      stream see the removal without re-fetching the pairings
      snapshot.

    Delivered to clients via the existing global
    ``subscribe_events`` stream — no separate subscription
    channel.

    Carries ``pin_sha256`` as the canonical identifier (offloader-
    side state is keyed on pin, not ``(hostname, port)``); receiver
    coords stay on the payload as display fields the frontend can
    show without a follow-up lookup.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    status: Literal["approved", "removed"]


class OffloaderPairEndpointReboundData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND``.

    Fired by the offloader's mDNS auto-rebind path
    (``OffloaderController._probe_and_rebind_endpoint``) after
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
    (``OffloaderController._apply_pair_status_result``) when a
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

    Carries ``pin_sha256`` as the canonical row identifier
    (``_offloader_alerts`` is keyed on pin); receiver coordinates
    stay on the payload as display fields. No discriminator on
    *which* resolution path got us here — the user-facing outcome
    (the alert disappears) is the same either way.
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
    (``OffloaderController._apply_pair_status_result``) when
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
    alert plumbing surface a different CTA ("contact the receiver
    admin") versus a pin-mismatch alert ("re-pair right now to
    pick up the new identity"). The operator action differs.

    The ``receiver_label`` is carried so the alert can name the
    row even after the pairings list has dropped it.
    ``pin_sha256`` carries the row's primary key (offloader-side
    state is keyed on pin) so the controller's listener has a
    direct lookup. No extra diagnostic detail; the receiver
    doesn't tell us *why* REJECTED, and the offloader can't
    distinguish admin-Reject from window-close from
    row-never-existed at this layer.
    """

    receiver_hostname: str
    receiver_port: int
    receiver_label: str
    pin_sha256: str


class RemoteBuildHostAddedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_HOST_ADDED``.

    Carries the full :class:`RemoteBuildPeer` projection of an
    mDNS-discovered (or refreshed) peer dashboard. Fires from
    :meth:`OffloaderController._on_service_state_change`'s
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

    ``pin_sha256`` is the canonical offloader-side row key;
    receiver coords stay on the payload as display fields the
    frontend can render without a follow-up lookup.

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

    ``pin_sha256`` is the canonical offloader-side row key;
    receiver coords stay on the payload as display fields.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    reason: str
    error_detail: str


class OffloaderQueueStatusChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_QUEUE_STATUS_CHANGED``.

    Fired on the offloader's local bus whenever the
    :class:`PeerLinkClient` receive loop processes an inbound
    ``queue_status`` application frame from a paired receiver.
    The remote-build controller listens, updates its
    RAM-only ``_peer_queue_status`` cache (keyed on
    ``pin_sha256``), and re-broadcasts via the global
    ``subscribe_events`` stream so frontend clients can render
    per-peer queue depth without polling. The scheduler reads
    the same cache to pick the least-busy peer on each new
    offload.
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

    Fires from :meth:`OffloaderController.set_offloader_settings`
    when the operator flips the master "Remote builds enabled"
    switch on the offloader Settings UI. Subscribers are the
    Settings UI on every connected tab — one toggle on one tab
    should flip the switch state on every other open tab without
    a refresh. The scheduler doesn't need this event because it
    reads :attr:`OffloaderController._remote_builds_enabled`
    on every install via :meth:`build_scheduler_snapshot`; the
    event is purely cross-tab UI sync.
    """

    remote_builds_enabled: bool


class OffloaderPairingEnabledChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIRING_ENABLED_CHANGED``.

    Fires from :meth:`OffloaderController.set_pairing_enabled`
    when the operator flips an individual paired-receiver
    enable switch on the offloader Settings UI. Subscribers
    update the matching row's switch state. The scheduler reads
    :attr:`StoredPairing.enabled` directly off the in-RAM
    ``_pairings`` dict via the snapshot, so no scheduler-side
    listener is needed; the event is the cross-tab UI sync seam.

    ``pin_sha256`` is the canonical row key; receivers
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
    Cached on :attr:`OffloaderController._offloader_remote_jobs`
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
