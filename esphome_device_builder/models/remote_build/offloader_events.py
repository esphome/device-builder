"""Offloader-side TypedDict event payloads + alert / snapshot wire shapes."""

from __future__ import annotations

from typing import Literal, TypedDict

from ...helpers.version_compat import VersionMatchPolicy


class OffloaderPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_STATUS_CHANGED``.

    Fires from the per-row pair-status listener when a PENDING
    :class:`StoredPairing` flips to APPROVED (admin Accept) or
    when the row is removed. Removal paths: the receiver returns
    REJECTED (admin Reject, window close, row never existed, or
    the *offloader's* own peer-link identity rotated), or the
    receiver returns APPROVED but the observed pubkey differs
    from the stored ``pin_sha256`` (receiver-side rotation; also
    fires ``OFFLOADER_PAIR_PIN_MISMATCH``). Also fires from
    ``unpair`` so other tabs on the global ``subscribe_events``
    stream see the removal without re-fetching the snapshot.

    ``pin_sha256`` is the canonical identifier — offloader-side
    state is pin-keyed, not ``(hostname, port)``.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    status: Literal["approved", "removed"]


class OffloaderPairingAddedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIRING_ADDED``.

    ``status`` is ``"pending"`` for a fresh request, ``"approved"`` on a
    re-pair short-circuit.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    label: str
    paired_at: float
    status: Literal["pending", "approved"]
    connected: bool
    connecting: bool
    last_connect_error: str
    esphome_version: str
    enabled: bool


class OffloaderPairEndpointReboundData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND``.

    Fires after the mDNS auto-rebind path observes a paired
    receiver broadcasting from a different ``(hostname, port)``
    than the stored row and a probe-before-mutate Noise XX
    handshake against the new endpoint confirms the pubkey hash
    still matches the stored ``pin_sha256``. The new peer-link
    client task is already respawned by firing time; its own
    ``OFFLOADER_PEER_LINK_OPENED`` follows in the same loop tick.
    """

    pin_sha256: str
    receiver_hostname: str
    receiver_port: int


class OffloaderPairPinMismatchData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_PIN_MISMATCH``.

    Fires when a Noise XX handshake returns
    ``IntentResponse.APPROVED`` but the observed pubkey hash
    differs from the stored ``pin_sha256`` — receiver identity
    rotated under us (legitimate rotation, or someone replacing
    the receiver). Fires *alongside* ``OFFLOADER_PAIR_STATUS_CHANGED
    status="removed"`` but carries the diagnostic detail
    (``expected_pin`` / ``observed_pin`` + ``receiver_label``)
    needed for the distinct "re-pair to confirm the new
    identity" CTA.

    ``pin_sha256`` (same value as ``expected_pin``) is
    duplicated as a separate field for direct primary-key
    lookup on the ``_offloader_alerts`` dict.
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

    Fires on the only two resolution paths that fix the
    underlying broken state — re-``request_pair`` against the
    same ``(hostname, port)``, or ``unpair`` removing the row.
    No operator-driven dismiss surface; clicking "OK" without
    acting would just hide a still-broken pairing.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str


class OffloaderPinMismatchAlert(TypedDict):
    """
    Snapshot row in the offloader-side alerts list.

    ``kind`` discriminates against :class:`OffloaderPeerRevokedAlert`
    so frontend subscribers branch to pick the alert copy + CTA.
    ``fired_at`` is wall-clock unix; snapshot order is dict
    insertion order (an upsert preserves the existing slot, so
    re-fires don't reshuffle the list). Frontends sort
    "newest first" themselves.
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
    """Snapshot row in the offloader-side alerts list (peer-revoked variant)."""

    kind: Literal["peer_revoked"]
    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    receiver_label: str
    fired_at: float


# Sum type the snapshot list carries; the ``kind`` Literal
# narrows field access at the consumer.
OffloaderAlertSnapshotEntry = OffloaderPinMismatchAlert | OffloaderPeerRevokedAlert


class OffloaderPairPeerRevokedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_PEER_REVOKED``.

    Fires when a Noise XX handshake returns
    ``IntentResponse.REJECTED`` for a PENDING / APPROVED row.
    Could be admin Reject, window-close clearing the receiver's
    pending dict, our own peer-link identity rotated, or the
    receiver simply doesn't have the row (legitimate re-install)
    — all four collapse to "the receiver isn't going to talk to
    us"; the alert copy stays generic.

    Fires *alongside* ``OFFLOADER_PAIR_STATUS_CHANGED
    status="removed"`` but the distinct event lets the alert
    surface a different CTA ("contact the receiver admin") vs
    pin-mismatch's "re-pair right now".
    """

    receiver_hostname: str
    receiver_port: int
    receiver_label: str
    pin_sha256: str


class RemoteBuildHostAddedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_HOST_ADDED``.

    Carries the full :class:`RemoteBuildPeer` projection of an
    mDNS-discovered (or refreshed) peer dashboard. Upsert
    semantics — frontend keys on ``name`` (mDNS service-instance
    name) and replaces an existing row with the same key.

    ``friendly_name``, ``pin_sha256`` and ``remote_build_port``
    come from the ``_esphomebuilder._tcp.local.`` TXT record;
    empty / 0 for receivers that don't broadcast them.
    """

    name: str
    hostname: str
    port: int
    source: str
    addresses: list[str]
    server_version: str
    esphome_version: str
    friendly_name: str
    pin_sha256: str
    remote_build_port: int


class RemoteBuildHostRemovedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_HOST_REMOVED``.

    Fires when zeroconf delivers a ``Removed`` event (TTL expiry
    without renewal, or an explicit goodbye). ``name`` matches
    the corresponding :class:`RemoteBuildHostAddedData`.
    """

    name: str


class OffloaderPeerLinkOpenedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PEER_LINK_OPENED``.

    Fires once a peer-link session reaches the post-handshake
    ``intent_response: ok`` state and the dispatch loop is
    parked. ``esphome_version`` is the receiver's
    :data:`esphome.const.__version__` lifted off the response;
    empty if the receiver didn't carry the field.
    ``auto_provision_supported`` is the receiver's capability flag
    (``False`` for an older receiver that never sent it). The
    controller subscribes to refresh both onto the
    :class:`StoredPairing` so pick_build_path's version-compat
    gate sees fresh values.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    esphome_version: str
    auto_provision_supported: bool


class OffloaderPeerLinkClosedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PEER_LINK_CLOSED``.

    ``reason`` is a wire value from :class:`TerminateReason`
    when receiver-initiated (``"superseded"`` /
    ``"server_shutting_down"`` / ``"heartbeat_timeout"`` /
    ``"malformed_frame"``) or an offloader-side reason when our
    side initiated (``"transport_error"`` /
    ``"heartbeat_timeout"`` / ``"client_stopped"`` /
    ``"peer_hung_up"`` / ``"auth_rejected"`` /
    ``"receiver_rejected"`` / ``"pin_mismatch"``). The peer-link
    client's reconnect logic branches on this; ``"superseded"``,
    ``"receiver_rejected"`` and ``"pin_mismatch"`` orphan.

    ``error_detail`` is one-line operator-facing context for
    ``"transport_error"`` (the exception text),
    ``"auth_rejected"`` / ``"receiver_rejected"`` (the rejection
    description), and ``"pin_mismatch"``. Empty for the remaining
    reasons, where the category name is the full explanation.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    reason: str
    error_detail: str


class OffloaderQueueStatusChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_QUEUE_STATUS_CHANGED``.

    Fires on every inbound ``queue_status`` frame from a paired
    receiver. The remote-build controller updates its pin-keyed
    ``_peer_queue_status`` cache and re-broadcasts via
    ``subscribe_events`` so frontends render per-peer depth
    without polling. The scheduler reads the same cache to pick
    the least-busy peer.
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

    Fires on every inbound ``job_state_changed`` frame from the
    receiver we submitted ``job_id`` to. Adds the source-receiver
    coordinates so subscribers can disambiguate transitions
    across multiple paired receivers. ``error_message`` is
    empty on non-terminal states and on ``completed``;
    populated on ``failed`` / ``cancelled``.
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

    Fires per inbound ``job_output`` frame. ``line`` carries
    its trailing terminator unchanged (``\n`` / ``\r`` /
    ``\r\n``) — carriage-return-only chunks are esptool /
    PlatformIO progress overwrites that the renderer needs to
    decide append-vs-overwrite. Do not strip.
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

    Cross-tab UI sync for the master "Remote builds enabled"
    switch. The scheduler doesn't need this event — it reads
    :attr:`OffloaderController._remote_builds_enabled` directly
    on every install via :meth:`build_scheduler_snapshot`.
    """

    remote_builds_enabled: bool


class OffloaderPairingEnabledChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIRING_ENABLED_CHANGED``.

    Cross-tab UI sync for an individual paired-receiver enable
    switch. The scheduler reads :attr:`StoredPairing.enabled`
    directly off the in-RAM ``_pairings`` dict via the
    snapshot, so no scheduler-side listener is needed.

    ``(hostname, port)`` aren't on the payload — frontends that
    need them join through their own
    :class:`PairingSummary` snapshot.
    """

    pin_sha256: str
    enabled: bool


class OffloaderVersionMatchPolicyChangedData(TypedDict):
    """Payload for ``EventType.OFFLOADER_VERSION_MATCH_POLICY_CHANGED``."""

    version_match_policy: VersionMatchPolicy


class OffloaderIncludeLocalChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_INCLUDE_LOCAL_CHANGED``.

    Cross-tab UI sync for the "include local in build pool"
    advanced toggle. The dispatcher reads the value directly off
    the in-RAM state via :meth:`build_scheduler_snapshot`, so no
    scheduler-side listener is needed.
    """

    include_local_in_pool: bool


class OffloaderSettingsSnapshot(TypedDict):
    """Offloader-wide scalars merged into the ``subscribe_events`` seed."""

    remote_builds_enabled: bool
    version_match_policy: VersionMatchPolicy
    include_local_in_pool: bool


class OffloaderRemoteJobSnapshotEntry(TypedDict):
    """
    Snapshot row in the offloader-side in-flight remote-job cache.

    Surfaced via ``subscribe_events.initial_state.remote_jobs``
    so a tab subscribing AFTER a ``running`` transition still
    sees the job alive without waiting for the next event.
    Terminal entries (``completed`` / ``failed`` / ``cancelled``)
    are dropped from the cache on the matching event — a page
    reload after a build completes shows no entry; the frontend
    keeps history itself if needed.
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

    Surfaced via
    ``subscribe_events.initial_state.peer_queue_status`` so a
    tab subscribing AFTER an event still sees the most recent
    per-paired-peer queue depth without waiting for the next
    live event.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    idle: bool
    running: bool
    queue_depth: int
