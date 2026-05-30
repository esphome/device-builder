"""Event-bus + per-stream protocol enums.

Split out of :mod:`.common` to keep that module's grab-bag of
shared types under the project's module-size cap. No sibling
imports — depends only on :class:`enum.StrEnum` — so the
models dependency graph stays acyclic.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["EventType", "StreamEvent"]


class EventType(StrEnum):
    """Events pushed to connected clients via subscribe_events."""

    # Device config file changes (detected by disk scanner)
    DEVICE_ADDED = "device_added"
    DEVICE_REMOVED = "device_removed"
    DEVICE_UPDATED = "device_updated"

    # Device online/offline state changes
    DEVICE_STATE_CHANGED = "device_state_changed"

    # Per-device reachability detail change — fired alongside
    # ``DEVICE_STATE_CHANGED`` (and on its own when only the per-signal
    # last-seen / rtt move) for any subscriber filtering by device.
    # The drawer subscribes to these via the ``devices/subscribe_reachability``
    # WS command; the broadcast ``subscribe_events`` excludes this
    # type explicitly (see ``_cmd_subscribe_events``) so a connected
    # client doesn't receive a freshness event for every device on
    # every mDNS announce.
    DEVICE_REACHABILITY = "device_reachability"

    # Discoverable device changes
    IMPORTABLE_DEVICE_ADDED = "importable_device_added"
    IMPORTABLE_DEVICE_REMOVED = "importable_device_removed"

    # Label catalog mutations. Per-device label assignment changes
    # ride the existing ``DEVICE_UPDATED`` event (fired automatically
    # by the scanner reload after the sidecar write).
    LABEL_CREATED = "label_created"
    LABEL_UPDATED = "label_updated"
    LABEL_DELETED = "label_deleted"

    # Firmware job lifecycle
    JOB_QUEUED = "job_queued"
    JOB_STARTED = "job_started"
    JOB_OUTPUT = "job_output"
    JOB_PROGRESS = "job_progress"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"

    # Receiver rotated its X25519 peer-link identity via
    # ``remote_build/rotate_identity``. Payload carries
    # ``{dashboard_id, pin_sha256}``: subscribers (the offloader-
    # side peer-link, the receiver Settings UI) can refresh
    # their cached pin without polling ``get_identity``. The
    # event fires after the on-disk rotation succeeds; the
    # listener rebuild may still fail-soft, in which case the
    # rotater's ``IdentityView`` response carries
    # ``listener_bound=False`` while the event itself reflects
    # only that the persistent key on disk changed.
    REMOTE_BUILD_IDENTITY_ROTATED = "remote_build_identity_rotated"

    # A pair_request Noise frame landed for a previously-unknown
    # peer while the receiver's pairing window was open (see
    # issue #106 design choice (b)/(c)). Payload:
    # ``{dashboard_id, pin_sha256, label, peer_ip}``. The receiver
    # Settings UI surfaces this in the Pairing requests inbox.
    # Fires only when the pairing window is open; closed-window
    # pair_requests are rejected at the listener with
    # ``intent_response=no_pairing_window`` and don't create a
    # row. The peer-link listener is the actual emitter.
    REMOTE_BUILD_PAIR_REQUEST_RECEIVED = "remote_build_pair_request_received"

    # A peer entry's status changed. Payload:
    # ``{dashboard_id, status: "approved" | "removed"}``. Fires
    # from three paths: (a) ``remote_build/approve_peer``
    # promoting a PENDING in-memory dict entry to APPROVED on
    # disk (``status="approved"``), (b) ``remote_build/remove_peer``
    # dropping either a PENDING dict entry or an APPROVED list
    # row (``status="removed"``), (c) pairing-window-close
    # clearing the in-memory PENDING dict (``status="removed"``
    # per cleared entry). The ``status="removed"`` event is
    # what wakes any in-flight ``intent="pair_status"`` long-poll
    # on a paired offloader so its listener task drops the
    # offloader's local state.
    #
    # Receiver Settings UI updates the inbox + approved-peers
    # list on this event. The offloader-side counterpart event
    # is :attr:`OFFLOADER_PAIR_STATUS_CHANGED`, fired on the
    # offloader's local bus by the offloader's pair-status
    # listener task after observing the receiver's response; the
    # two share a wire shape but live on different buses
    # (receiver vs offloader) and carry different identifiers
    # (offloader's dashboard_id vs the offloader's own
    # ``(receiver_hostname, receiver_port)`` coordinates).
    REMOTE_BUILD_PAIR_STATUS_CHANGED = "remote_build_pair_status_changed"

    # Offloader-side counterpart to ``REMOTE_BUILD_PAIR_STATUS_CHANGED``.
    # Payload: ``{receiver_hostname, receiver_port,
    # status: "approved" | "removed"}``. Fired by the offloader's
    # per-row pair-status listener task
    # (``OffloaderController._fire_offloader_pair_status_changed``,
    # called from ``_apply_pair_status_result`` once an
    # ``intent="pair_status"`` round-trip resolves), and also by
    # ``OffloaderController.unpair`` when the user removes a
    # row. Delivered to clients via the existing global
    # ``subscribe_events`` stream — no separate subscription
    # channel. Receiver-side keys aren't carried because the
    # offloader's :class:`StoredPairing` never stores the
    # receiver's ``dashboard_id`` — the receiver coordinates the
    # offloader knows are the ``(hostname, port)`` it dialled.
    OFFLOADER_PAIR_STATUS_CHANGED = "offloader_pair_status_changed"

    # Offloader-side mDNS auto-rebind. Payload: ``{pin_sha256,
    # receiver_hostname, receiver_port}``. Fired when the
    # offloader's mDNS browser observes a known paired receiver
    # broadcasting from a different ``(hostname, port)`` than the
    # ``StoredPairing`` records, and a probe-before-mutate Noise
    # XX handshake against the new endpoint confirmed the
    # responder's static pubkey matches the stored pin. The
    # controller has already mutated ``StoredPairing`` in place,
    # cancelled the old peer-link client, and respawned a fresh
    # one against the new coordinates by the time this fires;
    # frontends update the row's display fields off the new
    # ``receiver_hostname`` / ``receiver_port`` without a
    # re-fetch.
    OFFLOADER_PAIR_ENDPOINT_REBOUND = "offloader_pair_endpoint_rebound"

    # Pairing window opened, extended, or closed. Payload:
    # ``{open: bool, expires_in_seconds: float | None}``. Fires
    # on every state transition: window open (open=true,
    # expires_in_seconds=300), activity-driven extension
    # (open=true, expires_in_seconds=300 again, deadline
    # bumped), explicit close from the frontend (open=false,
    # expires_in_seconds=null), or auto-close on deadline reach
    # (open=false). The receiver frontend renders a live
    # countdown from ``expires_in_seconds``; idempotent calls
    # that don't change state (e.g. close-while-already-closed)
    # do NOT fire the event.
    REMOTE_BUILD_PAIRING_WINDOW_CHANGED = "remote_build_pairing_window_changed"

    # An mDNS-discovered peer dashboard appeared (or its TXT /
    # SRV info was refreshed). Payload: a full
    # :class:`RemoteBuildPeer` dict. The receiver-side controller
    # holds the discovered set in RAM (``self._peers``) and fires
    # this event from ``_on_service_state_change`` /
    # ``_resolve_and_apply`` whenever the row is upserted; the
    # frontend's pair-dialog "discovered dashboards" list mutates
    # against this stream rather than re-polling. The
    # ``subscribe_events`` initial-state push carries the full
    # current set under ``hosts`` so a fresh tab paints without a
    # round-trip.
    REMOTE_BUILD_HOST_ADDED = "remote_build_host_added"

    # An mDNS-discovered peer dashboard left the LAN (zeroconf
    # ``Removed`` callback — TTL expiry without renewal, or an
    # explicit goodbye). Payload: ``{name: str}`` matching the
    # service-instance name carried on the corresponding
    # ``REMOTE_BUILD_HOST_ADDED`` event. The frontend drops the
    # row from its discovered set on this event.
    REMOTE_BUILD_HOST_REMOVED = "remote_build_host_removed"

    # An offloader-side ``PeerLinkClient`` successfully
    # established a long-lived peer-link Noise WS session against
    # an APPROVED receiver — handshake completed, post-handshake
    # ``intent_response: ok`` landed, the dispatch loop is
    # parked waiting for application frames. Payload:
    # ``{receiver_hostname, receiver_port}``. Receiver
    # coordinates rather than ``dashboard_id`` because the
    # offloader's :class:`StoredPairing` keys on
    # ``(hostname, port)`` (the user dialled those) and the
    # offloader-side frontend Settings UI shows one row per
    # paired receiver. Subscribers update their per-receiver
    # ``connected`` indicator on this event.
    OFFLOADER_PEER_LINK_OPENED = "offloader_peer_link_opened"

    # Counterpart to :attr:`OFFLOADER_PEER_LINK_OPENED`. Fires
    # on every clean exit of a peer-link session: WS close,
    # heartbeat timeout, receiver-side ``terminate`` frame,
    # transport error during the receive loop. Payload:
    # ``{receiver_hostname, receiver_port, reason}`` where
    # ``reason`` is one of the receiver-side
    # :class:`TerminateReason` wire values
    # (``"superseded"`` / ``"server_shutting_down"`` /
    # ``"heartbeat_timeout"`` / ``"malformed_frame"``) when
    # the close came from a structured ``terminate`` frame,
    # or one of the offloader-side reasons
    # (``"transport_error"`` / ``"heartbeat_timeout"`` /
    # ``"client_stopped"`` / ``"peer_hung_up"`` /
    # ``"auth_rejected"``) when our side initiated. The
    # reconnect logic in
    # :class:`PeerLinkClient` branches on this so a
    # ``superseded`` close doesn't trigger a reconnect storm.
    OFFLOADER_PEER_LINK_CLOSED = "offloader_peer_link_closed"

    # Receiver-side counterpart to :attr:`OFFLOADER_PEER_LINK_OPENED`.
    # Fires from :meth:`ReceiverController.register_peer_link_session`
    # the moment a peer-link Noise WS session lands in the
    # receiver's ``_peer_link_sessions`` registry — i.e. the
    # post-handshake ``_run_peer_link_session`` has installed the
    # session and is about to enter its dispatch loop. Payload:
    # ``{dashboard_id}`` (the offloader's stable identity, as
    # captured during the Noise XX handshake).
    #
    # Subscribers: 5b's ``queue_status`` push uses this as the
    # signal to send the initial queue snapshot to a freshly-
    # connected offloader (no lookup-then-push race window where
    # a queue transition fires between handshake completion and
    # session registration). The receiver-side frontend Settings
    # UI uses it to render a "connected" indicator per offloader
    # in the approved-peers list.
    RECEIVER_PEER_LINK_SESSION_OPENED = "receiver_peer_link_session_opened"

    # Counterpart to :attr:`RECEIVER_PEER_LINK_SESSION_OPENED`.
    # Fires from :meth:`ReceiverController.unregister_peer_link_session`
    # when the receiver's session loop unwinds (offloader
    # disconnects, heartbeat timeout, controller shutdown,
    # ``superseded`` eviction). Payload: ``{dashboard_id}``.
    #
    # No ``reason`` field on this side: the receiver sees only
    # "the session loop returned"; the rich reason classification
    # lives on the offloader side (``OFFLOADER_PEER_LINK_CLOSED``)
    # because that's where the close path's branches diverge
    # (transport error vs heartbeat timeout vs structured
    # terminate frame). Receiver-side subscribers don't need to
    # discriminate.
    RECEIVER_PEER_LINK_SESSION_CLOSED = "receiver_peer_link_session_closed"

    # Offloader-side detection: the receiver's static X25519
    # pubkey hash observed during a pair-status / peer-link
    # handshake doesn't match the ``StoredPairing.pin_sha256``
    # the offloader recorded at pair time. The receiver's
    # identity rotated under us (legitimate
    # ``rotate_peer_link_identity`` from the receiver-side
    # admin, or someone replacing the receiver). Payload:
    # ``{receiver_hostname, receiver_port, receiver_label,
    # expected_pin, observed_pin}``. Fires alongside
    # ``OFFLOADER_PAIR_STATUS_CHANGED status="removed"`` (the
    # row drops either way); subscribers use this event to
    # surface the "re-pair to confirm the new identity" alert
    # in the offloader's UI distinct from a peer-revocation
    # alert. No receiver-side counterpart — the receiver never
    # sees its own pin drift.
    OFFLOADER_PAIR_PIN_MISMATCH = "offloader_pair_pin_mismatch"

    # Offloader-side detection: the receiver actively returned
    # ``intent_response="rejected"`` on a pair-status long-poll
    # for a row the offloader had as PENDING / APPROVED.
    # Receiver admin clicked Reject, the pairing window closed
    # clearing the receiver's pending dict, the offloader's
    # identity rotated, or the receiver never had this row.
    # Payload: ``{receiver_hostname, receiver_port,
    # receiver_label}``. Fires alongside
    # ``OFFLOADER_PAIR_STATUS_CHANGED status="removed"``;
    # subscribers use this event for the "the receiver removed
    # us; reach out if this was a mistake" alert distinct from
    # a pin-mismatch alert (different operator response —
    # pin-mismatch can be re-paired right away, peer-revoked
    # needs receiver-side admin coordination).
    OFFLOADER_PAIR_PEER_REVOKED = "offloader_pair_peer_revoked"

    # An offloader-side pair alert was cleared by one of the
    # two resolution paths that fix the underlying broken
    # state: a successful ``request_pair`` against the same
    # ``(hostname, port)`` (re-pair auto-resolved the alert),
    # or ``unpair`` removing the row outright. There is no
    # operator-driven dismiss — clicking "OK got it" without
    # acting would just hide a broken pairing the next peer-
    # link session would still fail against, so re-pair and
    # unpair are the only ways the alert clears. Payload:
    # ``{receiver_hostname, receiver_port}``. RAM-only state
    # on the controller's ``_offloader_alerts`` dict; the
    # event keeps other tabs / clients on the global
    # ``subscribe_events`` stream in sync without re-fetching
    # the alerts snapshot. Late-subscribing clients pick up
    # the canonical state via
    # ``subscribe_events.initial_state.offloader_alerts``.
    OFFLOADER_PAIR_ALERT_DISMISSED = "offloader_pair_alert_dismissed"

    # Offloader-side cache update: a paired receiver pushed a
    # fresh ``queue_status`` snapshot over its peer-link
    # session. Payload: ``{receiver_hostname, receiver_port,
    # idle, running, queue_depth}``. Fired from the
    # offloader-side ``PeerLinkClient`` receive loop on every
    # inbound ``queue_status`` application frame; the
    # remote-build controller listens, updates its
    # ``_peer_queue_status`` cache (RAM-only, keyed on
    # ``(host, port)``), and re-broadcasts via the global
    # ``subscribe_events`` stream so frontend clients can
    # render the per-peer queue depth live without polling.
    # The scheduler reads the same cache.
    OFFLOADER_QUEUE_STATUS_CHANGED = "offloader_queue_status_changed"

    # Offloader-side: a paired receiver pushed a
    # ``job_state_changed`` application frame for a job we
    # submitted. Payload:
    # ``{receiver_hostname, receiver_port, pin_sha256, job_id,
    # status, error_message}``. ``status`` mirrors the wire
    # frame's literal (``queued`` / ``running`` / ``completed`` /
    # ``failed`` / ``cancelled``). The remote-build controller
    # re-broadcasts via the global ``subscribe_events`` stream
    # so frontend tabs see the lifecycle of a remote build live.
    # Distinct from the local :attr:`JOB_STARTED` /
    # :attr:`JOB_COMPLETED` family because remote-driven jobs
    # don't have a corresponding :class:`FirmwareJob` row on
    # the offloader — the receiver owns the queue state and we
    # only see the wire reflection.
    OFFLOADER_JOB_STATE_CHANGED = "offloader_job_state_changed"

    # Offloader-side: a paired receiver pushed a ``job_output``
    # application frame for a job we submitted. Payload:
    # ``{receiver_hostname, receiver_port, pin_sha256, job_id,
    # stream, line}`` — ``stream`` is ``stdout`` / ``stderr``,
    # ``line`` preserves its trailing terminator (carriage-return
    # vs newline carries semantic info; the same contract the
    # local :class:`JobOutputData` event holds). Frames flow at
    # high rate during an active build (one per line of compiler
    # / linker output); subscribers should debounce / batch
    # downstream rendering rather than re-rendering per event.
    OFFLOADER_JOB_OUTPUT = "offloader_job_output"

    # Offloader-side master toggle changed. Fires from
    # :meth:`OffloaderController.set_offloader_settings`
    # whenever the operator flips the "Remote builds enabled"
    # switch in the offloader Settings UI. Payload:
    # ``{remote_builds_enabled: bool}``. Subscribers are the
    # Settings UI (renders the live switch state) — the
    # scheduler doesn't need an event because it reads
    # :attr:`OffloaderController._remote_builds_enabled` on
    # every install via :meth:`build_scheduler_snapshot`. The
    # event still fires so a second open tab sees the
    # cross-tab toggle without polling.
    OFFLOADER_REMOTE_BUILDS_TOGGLED = "offloader_remote_builds_toggled"

    # Offloader-side per-pairing toggle changed. Fires
    # from :meth:`OffloaderController.set_pairing_enabled`
    # whenever the operator flips an individual paired
    # receiver's enable switch. Payload:
    # ``{pin_sha256: str, enabled: bool}``. Subscribers
    # update the matching row's switch in the offloader
    # Settings UI; the scheduler reads
    # :attr:`StoredPairing.enabled` directly off the in-RAM
    # ``_pairings`` dict via the snapshot.
    OFFLOADER_PAIRING_ENABLED_CHANGED = "offloader_pairing_enabled_changed"

    # Cross-tab sync for the master version-match policy.
    OFFLOADER_VERSION_MATCH_POLICY_CHANGED = "offloader_version_match_policy_changed"


class StreamEvent(StrEnum):
    """Per-stream frame names sent via ``WebSocketClient.send_event``.

    Distinct from :class:`EventType` (the global event-bus channel
    name): a ``StreamEvent`` is the ``event`` field of a single
    streaming command's response frames (``follow_job``,
    ``stream_logs``, ``validate_config``, ``follow_jobs``'s initial
    snapshot). Two-tier model — bus events get fanned out to per-
    connection streams, where the controller may relabel them
    (e.g. ``EventType.JOB_OUTPUT`` becomes ``StreamEvent.OUTPUT``
    inside a ``follow_job`` stream that's already scoped to a
    specific job_id).

    The wire bytes coincide with some ``EventType`` values
    (``"job_output"``, ``"job_progress"``) for the all-jobs
    follower path, where the stream simply forwards the bus event
    name through. Those call sites pass the ``EventType`` member
    directly (it's a ``StrEnum``, so it serialises to the same
    string) rather than redeclaring the constant here.
    """

    # Per-line subprocess output (``follow_job`` / ``stream_logs`` /
    # ``validate_config``).
    OUTPUT = "output"
    # Terminal frame — final status / exit code, ends a streaming
    # command. Sent priority so a backlog of output frames can't
    # drop the close signal.
    RESULT = "result"
    # Initial replay of buffered state at the start of a stream
    # (``follow_jobs`` snapshots the job table; ``follow_job``
    # replays the job's output ring before live tail).
    SNAPSHOT = "snapshot"
