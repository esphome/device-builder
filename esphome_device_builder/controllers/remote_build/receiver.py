"""
Receiver-side controller for the remote-build feature.

Owns the dashboard's *inbound* role: accepting paired
offloaders, persisting the per-``dashboard_id``
:class:`StoredPeer` table, gating new pair requests behind a
pairing window, accepting peer-link sessions, and fanning out
firmware ``JOB_*`` events back to whichever offloader
submitted each remote job.

Pairs with :class:`~.offloader.OffloaderController` — the two
siblings own disjoint state and never reach across; the only
shared coupling is :mod:`._shared` (a free
:func:`drain_tasks` helper) and the
:class:`~esphome_device_builder.device_builder.DeviceBuilder`
reference passed to both at construction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Hashable
from typing import TYPE_CHECKING, Any, Literal

from ...helpers.api import CommandError, api_command
from ...helpers.event_bus import Event
from ...helpers.storage import Store
from ...models import (
    MAX_CLEANUP_TTL_SECONDS,
    MIN_CLEANUP_TTL_SECONDS,
    TERMINAL_JOB_EVENTS,
    ErrorCode,
    EventType,
    IdentityView,
    IntentResponse,
    PairingWindowState,
    PeerStatus,
    PeerSummary,
    ReceiverPeers,
    RemoteBuildPairingWindowChangedData,
    RemoteBuildPairRequestReceivedData,
    RemoteBuildPairStatusChangedData,
    RemoteBuildSettings,
    RemoteBuildSettingsView,
    StoredPeer,
)
from ..config import (
    load_remote_build_settings,
    remote_build_settings_transaction,
)
from . import cleanup_loop, identity_commands, peer_link_sessions
from ._receiver_state import ReceiverState
from ._shared import _RemoteBuildBase, drain_tasks
from ._storage_codecs import (
    RECEIVER_PEERS_FILE,
    decode_peers,
    encode_peers,
)
from ._summaries import peer_summary
from ._validators import (
    validate_dashboard_id,
)
from .artifacts_download import ArtifactsDownloadSender
from .job_fanout import JobFanout
from .peer_link import PeerLinkSession, TerminateReason
from .submit_job import SubmitJobReceiver

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)


# Pairing-window lifetime. Auto-closes after this much idle;
# the frontend extends on each activity tick.
_PAIRING_WINDOW_DURATION_SECONDS = 300.0

# Debounce window for the receiver-side peers-store write so a
# burst of approvals collapses to one disk write.
_PEERS_SAVE_DELAY_SECONDS = 1.0


class ReceiverController(_RemoteBuildBase):  # noqa: PLR0904
    """Inbound side of remote-build: pair inbox, peer-link sessions, JOB_* fan-out."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        super().__init__(device_builder)
        self.state = ReceiverState()
        self._peers_store: Store[ReceiverPeers] = Store(
            self._db.settings.config_dir / RECEIVER_PEERS_FILE,
            encoder=encode_peers,
            decoder=decode_peers,
            shutdown_register=self._shutdown_callbacks.append,
            name="receiver_peers",
        )

    async def start(self) -> None:
        """Bring up the receiver-side handlers, seed RAM from disk."""
        if self._db.firmware is not None:
            self.state.submit_job_receiver = SubmitJobReceiver(
                config_dir=self._db.settings.config_dir,
                firmware_controller=self._db.firmware,
            )
            self.state.artifacts_download_sender = ArtifactsDownloadSender(
                firmware_controller=self._db.firmware,
            )
            self.state.job_fanout = JobFanout(self)
            self.state.job_fanout.start()
            self._track_task(
                self._run_cleanup_loop(),
                name=f"{type(self).__name__}._run_cleanup_loop",
            )
        if (peers_state := await self._peers_store.async_load()) is not None:
            for peer in peers_state.peers:
                self.state.approved_peers[peer.dashboard_id] = peer
        # JOB_OUTPUT / JOB_PROGRESS deliberately omitted: high-rate
        # streaming events that don't change queue_status shape.
        for event_type in (
            EventType.JOB_QUEUED,
            EventType.JOB_STARTED,
            *TERMINAL_JOB_EVENTS,
        ):
            self._listeners.callback(
                self._db.bus.add_listener(event_type, self._on_firmware_queue_transition)
            )

    async def stop(self) -> None:
        """Close listeners, terminate sessions, drain tasks, flush store."""
        self._listeners.close()
        if self.state.job_fanout is not None:
            self.state.job_fanout.stop()
            self.state.job_fanout = None
        # Drop the receiver-side handler refs so a subsequent
        # ``get_*`` call after ``stop()`` fails its
        # ``RuntimeError`` guard cleanly instead of returning a
        # stale firmware-controller-bound instance.
        self.state.submit_job_receiver = None
        self.state.artifacts_download_sender = None
        await drain_tasks(self._tasks)
        self._tasks.clear()
        if self.state.pairing_window_handle is not None:
            self.state.pairing_window_handle.cancel()
            self.state.pairing_window_handle = None
        self.state.pairing_window_clients.clear()
        # Snapshot to a list before iterating — each terminate
        # unwinds via ``unregister_peer_link_session`` which
        # mutates the dict.
        for peer_link_session in list(self.state.peer_link_sessions.values()):
            await peer_link_session.terminate(TerminateReason.SERVER_SHUTTING_DOWN)
        self.state.peer_link_sessions.clear()
        # Fire ``status="removed"`` for each PENDING peer so
        # in-flight pair_status long-polls on a still-alive bus
        # see the cancellation (matters for the soft-reload path).
        self._clear_pending_peers_on_window_close()
        for callback in self._shutdown_callbacks:
            await callback()
        self.state.approved_peers.clear()

    async def _load_settings_async(self) -> RemoteBuildSettings:
        """Read the receiver-side settings sidecar off the executor.

        Carries the ``enabled`` master toggle +
        ``cleanup_ttl_seconds`` knobs, which aren't mirrored in
        RAM (the RAM-canonical state is
        :attr:`ReceiverState.approved_peers` /
        :attr:`ReceiverState.pending_peers`).
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )

    def _on_firmware_queue_transition(self, event: Event[Any]) -> None:
        """Bus listener: broadcast ``queue_status`` to paired offloaders."""
        peer_link_sessions.on_firmware_queue_transition(self, event)

    async def register_peer_link_session(self, session: PeerLinkSession) -> None:
        """Register *session*; evict a stale same-``dashboard_id`` slot via SUPERSEDED."""
        await peer_link_sessions.register_peer_link_session(self, session)

    def unregister_peer_link_session(self, session: PeerLinkSession) -> None:
        """Drop *session* from the active peer-link registry."""
        peer_link_sessions.unregister_peer_link_session(self, session)

    async def handle_cancel_job(self, session: PeerLinkSession, frame: dict[str, Any]) -> None:
        """Receiver-side dispatch for inbound ``cancel_job`` frames."""
        await peer_link_sessions.handle_cancel_job(self, session, frame)

    def get_submit_job_receiver(self) -> SubmitJobReceiver:
        """Return the receiver-side ``submit_job`` flow handler, raising if not started.

        Method (not ``@property``) because
        :func:`collect_api_commands` walks public attributes
        at startup; a property getter would fire pre-``start``
        and raise.
        """
        if self.state.submit_job_receiver is None:
            msg = "submit_job_receiver accessed before ReceiverController.start()"
            raise RuntimeError(msg)
        return self.state.submit_job_receiver

    def get_artifacts_download_sender(self) -> ArtifactsDownloadSender:
        """Return the receiver-side ``download_artifacts`` flow handler, raising if not started."""
        if self.state.artifacts_download_sender is None:
            msg = "artifacts_download_sender accessed before ReceiverController.start()"
            raise RuntimeError(msg)
        return self.state.artifacts_download_sender

    async def _run_cleanup_loop(self) -> None:
        """Sweep cold remote-build subtrees on a periodic cadence."""
        await cleanup_loop.run_cleanup_loop(self)

    @api_command("remote_build/get_settings")
    async def get_settings(self, **kwargs: Any) -> RemoteBuildSettingsView:
        """Return the receiver-side remote-build settings (wire view)."""
        return self._to_view(await self._load_settings_async())

    def _to_view(self, settings: RemoteBuildSettings) -> RemoteBuildSettingsView:
        """Project receiver settings to wire view, merging in-memory peers.

        The peer list is RAM-canonical: PENDING entries live in
        ``self.state.pending_peers`` for the active pairing window's
        lifetime (never hit disk) and APPROVED entries live in
        ``self.state.approved_peers`` / its per-file ``Store``.
        ``settings`` is consulted for the master ``enabled``
        toggle.
        """
        return RemoteBuildSettingsView(
            enabled=settings.enabled,
            cleanup_ttl_seconds=settings.cleanup_ttl_seconds,
            peers=self._peer_summaries(),
        )

    def _peer_summaries(self) -> list[PeerSummary]:
        """Merge PENDING + APPROVED into a single ``PeerSummary`` list.

        APPROVED rows read ``connected`` off
        ``state.peer_link_sessions``; PENDING always
        ``connected=False`` since the peer-link dispatch
        refuses non-APPROVED rows.
        """
        sessions = self.state.peer_link_sessions
        return [
            peer_summary(p, status=PeerStatus.PENDING, connected=False)
            for p in self.state.pending_peers.values()
        ] + [
            peer_summary(p, status=PeerStatus.APPROVED, connected=p.dashboard_id in sessions)
            for p in self.state.approved_peers.values()
        ]

    def approved_peer_label(self, dashboard_id: str) -> str:
        """Return the APPROVED peer's display label, or ``""`` if not found."""
        peer = self.state.approved_peers.get(dashboard_id)
        return peer.label if peer is not None else ""

    def peers_snapshot(self) -> list[PeerSummary]:
        """Return the in-memory peers (PENDING + APPROVED) for ``subscribe_events`` seeding."""
        return self._peer_summaries()

    async def _modify_settings(
        self, mutator: Callable[[RemoteBuildSettings], None]
    ) -> RemoteBuildSettingsView:
        """
        Run ``mutator`` against the current settings and persist the result.

        Wraps :func:`remote_build_settings_transaction` so the
        whole read-modify-write happens under the metadata lock,
        so two concurrent callers can't both read the same starting
        value and have the second save wipe the first's change.
        Runs in the default executor since the transaction does
        blocking JSON I/O. Returns the wire view so the response
        leaving this method can never carry ``secret_sha256``.

        ``mutator`` is invoked with the freshly-loaded settings
        and is expected to mutate it in place. A
        :class:`CommandError` raised inside the mutator (e.g.
        duplicate-detection on add) propagates out and discards
        the pending write; same exception-on-discard contract as
        :func:`metadata_transaction`.
        """

        def _txn() -> RemoteBuildSettings:
            with remote_build_settings_transaction(self._db.settings.config_dir) as settings:
                mutator(settings)
                return settings

        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(None, _txn)
        return self._to_view(settings)

    @api_command("remote_build/set_settings")
    async def set_settings(
        self,
        *,
        enabled: bool,
        cleanup_ttl_seconds: int | None = None,
        **kwargs: Any,
    ) -> RemoteBuildSettingsView:
        """
        Persist the receiver-side ``enabled`` master switch.

        Read-modify-write so peers / other fields stay intact.
        Strict-bool validation defeats truthiness coercion on
        this security-sensitive toggle.

        Optional ``cleanup_ttl_seconds`` updates the cleanup
        sweep threshold, range-checked against
        :data:`MIN_CLEANUP_TTL_SECONDS` /
        :data:`MAX_CLEANUP_TTL_SECONDS`. Omit to keep current.

        Live-rebinds the peer-link listener: True runs the
        startup bind path, False tears down + clears the mDNS
        pin/port advertise. Fail-soft on bind error.
        """
        if not isinstance(enabled, bool):
            msg = "remote_build/set_settings: 'enabled' must be a boolean"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        if cleanup_ttl_seconds is not None:
            # bool subclasses int, so reject ``True`` first to
            # avoid a misleading OUT_OF_RANGE on a type error.
            if isinstance(cleanup_ttl_seconds, bool) or not isinstance(cleanup_ttl_seconds, int):
                msg = "remote_build/set_settings: 'cleanup_ttl_seconds' must be an integer"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            if not MIN_CLEANUP_TTL_SECONDS <= cleanup_ttl_seconds <= MAX_CLEANUP_TTL_SECONDS:
                msg = (
                    f"remote_build/set_settings: 'cleanup_ttl_seconds' must be between "
                    f"{MIN_CLEANUP_TTL_SECONDS} and {MAX_CLEANUP_TTL_SECONDS}"
                )
                raise CommandError(ErrorCode.INVALID_ARGS, msg)

        def _set(settings: RemoteBuildSettings) -> None:
            settings.enabled = enabled
            if cleanup_ttl_seconds is not None:
                settings.cleanup_ttl_seconds = cleanup_ttl_seconds

        view = await self._modify_settings(_set)
        await self._db.apply_remote_build_enabled()
        return view

    # ------------------------------------------------------------------
    # Offloader-side settings: master toggle + per-pairing enable.
    # Mutations persist via the existing ``_pairings_store``.
    # ------------------------------------------------------------------

    @api_command("remote_build/get_identity")
    async def get_identity(self, **kwargs: Any) -> IdentityView:
        """Return this dashboard's stable identity (id + pin + versions + bind state)."""
        return await identity_commands.get_identity(self)

    @api_command("remote_build/rotate_identity")
    async def rotate_identity(self, **kwargs: Any) -> IdentityView:
        """Mint a fresh X25519 peer-link keypair, replacing whatever's on disk."""
        return await identity_commands.rotate_identity(self)

    # ------------------------------------------------------------------
    # Peer CRUD — receiver-UI surface for the Pairing requests inbox.
    # PENDING rows are created by the peer-link listener; these
    # commands let the admin act on them.
    # ------------------------------------------------------------------

    @api_command("remote_build/approve_peer")
    async def approve_peer(self, *, dashboard_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Promote a PENDING peer to APPROVED.

        Pops the in-memory PENDING entry, inserts it into the
        RAM-canonical ``state.approved_peers`` dict, schedules a
        debounced write to the receiver-peers store, and fires
        :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` with
        ``{dashboard_id, status: "approved"}``. The offloader's
        pair-status listener observes the flip via the bus event +
        re-snapshot path. ``NOT_FOUND`` if no PENDING entry
        matches; ``INVALID_ARGS`` if the dashboard_id already
        corresponds to an APPROVED row (duplicate Accept click,
        almost always a UI race; refuse rather than silently
        re-fire the event).
        """
        clean_id = validate_dashboard_id(dashboard_id)

        pending = self.state.pending_peers.pop(clean_id, None)
        if pending is None:
            # Differentiate "already approved" from "never existed"
            # so the frontend can decide whether to refresh or
            # surface an error. Both reads short-circuit through
            # RAM — no disk I/O.
            if clean_id in self.state.approved_peers:
                msg = f"peer is already approved: {clean_id}"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            msg = f"no pending peer with dashboard_id: {clean_id}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)

        self.state.approved_peers[clean_id] = pending
        self._peers_store.async_delay_save(self._serialize_peers, delay=_PEERS_SAVE_DELAY_SECONDS)
        self._fire_pair_status_changed(clean_id, "approved")
        return await self._current_settings_view()

    @api_command("remote_build/remove_peer")
    async def remove_peer(self, *, dashboard_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Delete a peer row (works on both PENDING and APPROVED).

        Two semantically distinct outcomes share the same WS command:

        * Removing a PENDING entry from the in-memory dict is
          *rejection* — the row never represented established
          trust, so this is inbox cleanup. Fires the
          ``status="removed"`` event so any offloader currently
          long-polling pair_status sees the cancellation and
          drops its local state.
        * Removing an APPROVED row from ``state.approved_peers``
          (RAM-canonical, debounced to disk) is *revocation* —
          fires the same
          :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED`
          ``status="removed"`` event so the offloader can
          surface a ``peer_revoked`` UI alert.

        ``NOT_FOUND`` if neither dict has a row.
        """
        clean_id = validate_dashboard_id(dashboard_id)

        # PENDING: in-memory, no disk write needed (PENDING never
        # reaches the peers store).
        if self.state.pending_peers.pop(clean_id, None) is not None:
            self._fire_pair_status_changed(clean_id, "removed")
            return await self._current_settings_view()

        if self.state.approved_peers.pop(clean_id, None) is None:
            msg = f"no peer with dashboard_id: {clean_id}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        self._peers_store.async_delay_save(self._serialize_peers, delay=_PEERS_SAVE_DELAY_SECONDS)
        self._fire_pair_status_changed(clean_id, "removed")
        return await self._current_settings_view()

    def _serialize_peers(self) -> ReceiverPeers:
        """Build the on-disk peers shape from the in-RAM ``state.approved_peers`` dict."""
        return ReceiverPeers(peers=list(self.state.approved_peers.values()))

    async def _current_settings_view(self) -> RemoteBuildSettingsView:
        """Load settings from disk and project to the wire view (post-mutation response)."""
        return self._to_view(await self._load_settings_async())

    # ------------------------------------------------------------------
    # Peer-link Noise WS dispatch helpers — called by the post-handshake
    # intent dispatcher in :mod:`controllers.remote_build_peer_link`.
    # These methods own the storage / event-firing side; the dispatcher
    # owns the wire side.
    # ------------------------------------------------------------------

    async def record_pair_request(
        self,
        *,
        dashboard_id: str,
        pin_sha256: str,
        static_x25519_pub: bytes,
        label: str,
        peer_ip: str,
    ) -> IntentResponse:
        """
        Process an ``intent="pair_request"`` Noise session.

        Returns:
        * ``APPROVED`` — row exists for ``dashboard_id`` with
          APPROVED status and matching pin. Re-pair against
          existing trust bypasses the pairing window so an
          offloader hiccup doesn't force a re-approve.
        * ``PENDING`` — new ``StoredPeer`` created or existing
          PENDING row refreshed. Only reachable inside an open
          pairing window; fires
          :attr:`EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED`
          so the receiver UI surfaces the inbox row.
        * ``REJECTED`` — APPROVED row exists but pin doesn't
          match: offloader rotated identity, or someone is
          claiming a stranger's ``dashboard_id``. Refused
          regardless of window state.
        * ``NO_PAIRING_WINDOW`` — closed window for a request
          that would create/refresh a PENDING row.
        """
        # Already-APPROVED row: re-pair against existing trust
        # bypasses the window. Pin mismatch is refused regardless
        # (rotation or impersonation).
        approved_peer = self.state.approved_peers.get(dashboard_id)
        if approved_peer is not None:
            if approved_peer.pin_sha256 != pin_sha256:
                return IntentResponse.REJECTED
            return IntentResponse.APPROVED

        if not self.is_pairing_window_open():
            return IntentResponse.NO_PAIRING_WINDOW

        # Refuse to overwrite a PENDING entry's pubkey — defense
        # in depth against a LAN attacker injecting a rival key
        # under the same scraped dashboard_id (the OOB fingerprint
        # check at approve-time is the load-bearing gate, but
        # silent overwrite enables a DoS). Same-pubkey retries
        # refresh label / peer_ip / paired_at via the path below.
        existing = self.state.pending_peers.get(dashboard_id)
        if existing is not None and existing.static_x25519_pub != static_x25519_pub:
            _LOGGER.warning(
                "pair_request from %s claims dashboard_id=%s but presented "
                "a different X25519 pubkey than the existing PENDING entry "
                "from %s; refusing the overwrite",
                peer_ip,
                dashboard_id,
                existing.peer_ip,
            )
            return IntentResponse.REJECTED

        paired_at = time.time()
        self.state.pending_peers[dashboard_id] = StoredPeer(
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            static_x25519_pub=static_x25519_pub,
            label=label,
            paired_at=paired_at,
            peer_ip=peer_ip,
        )
        payload: RemoteBuildPairRequestReceivedData = {
            "dashboard_id": dashboard_id,
            "pin_sha256": pin_sha256,
            "label": label,
            "peer_ip": peer_ip,
            "paired_at": paired_at,
        }
        self._db.bus.fire(EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED, payload)
        return IntentResponse.PENDING

    async def lookup_peer_for_session(
        self,
        *,
        dashboard_id: str,
        pin_sha256: str,
    ) -> IntentResponse:
        """
        Resolve an ``intent="peer_link"`` request.

        Returns ``OK`` if APPROVED + pin matches, ``PENDING`` if
        the row's still in the pending dict (admin hasn't clicked
        Accept), ``REJECTED`` for no row or pin drift. The
        offloader treats REJECTED as "send a fresh pair_request".
        """
        return await self._lookup_peer_response(
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            approved_response=IntentResponse.OK,
        )

    async def lookup_peer_for_status(
        self,
        *,
        dashboard_id: str,
        pin_sha256: str,
    ) -> IntentResponse:
        """
        Resolve an ``intent="pair_status"`` query, long-polling on PENDING.

        Returns :attr:`IntentResponse.APPROVED` or ``REJECTED``.
        REJECTED is reached four ways: never paired, admin
        clicked Reject, offloader's peer-link identity rotated,
        or window-close cleared the pending dict mid-wait. The
        offloader treats all of them as peer-revoked.

        Long-poll: with snapshot=PENDING, await
        :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` for
        the matching ``dashboard_id``. No timeout — WS hangs
        until the offloader cancels or the dict mutates.

        Listener-attach-before-snapshot ordering is
        load-bearing: an ``approve_peer`` firing between
        snapshot and wait must not slip past. Window-gating is
        implicit — closed window = empty pending dict = REJECTED
        on snapshot, long-poll never starts.

        Differs from :meth:`lookup_peer_for_session` only in
        returning ``APPROVED`` vs ``OK`` — pair_status is
        informational, peer_link is connection-establishing.
        """
        flip_event = asyncio.Event()

        def _on_pair_status(event: Event[RemoteBuildPairStatusChangedData]) -> None:
            if event.data["dashboard_id"] == dashboard_id:
                flip_event.set()

        with self._db.bus.listening([EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED], _on_pair_status):
            snapshot = await self._lookup_peer_response(
                dashboard_id=dashboard_id,
                pin_sha256=pin_sha256,
                approved_response=IntentResponse.APPROVED,
            )
            if snapshot is not IntentResponse.PENDING:
                return snapshot
            await flip_event.wait()
            return await self._lookup_peer_response(
                dashboard_id=dashboard_id,
                pin_sha256=pin_sha256,
                approved_response=IntentResponse.APPROVED,
            )

    async def _lookup_peer_response(
        self,
        *,
        dashboard_id: str,
        pin_sha256: str,
        approved_response: IntentResponse,
    ) -> IntentResponse:
        """
        Shared lookup core for the peer_link / pair_status WS dispatch paths.

        Walks the in-memory PENDING dict first, then the persisted
        APPROVED list. Both intents need the same pin-match check
        on either store; only the APPROVED return value differs
        (caller passes :attr:`IntentResponse.OK` for peer_link,
        :attr:`IntentResponse.APPROVED` for pair_status).

        Returns ``REJECTED`` when no row matches OR pin doesn't
        match — the offloader treats either case the same (drop
        local row + surface re-pair UI).
        """
        # PENDING dict first — most pair-flow traffic is pending
        # peers polling pair_status. Both lookups are RAM reads
        # (the APPROVED list moved off disk into
        # ``state.approved_peers`` at startup).
        pending = self.state.pending_peers.get(dashboard_id)
        if pending is not None:
            if pending.pin_sha256 != pin_sha256:
                return IntentResponse.REJECTED
            return IntentResponse.PENDING
        peer = self.state.approved_peers.get(dashboard_id)
        if peer is None or peer.pin_sha256 != pin_sha256:
            return IntentResponse.REJECTED
        return approved_response

    # ------------------------------------------------------------------
    # Pairing window — in-process deadline that gates
    # ``intent="pair_request"`` Noise frames at the listener (the
    # listener consumes :meth:`is_pairing_window_open`). See issue
    # #106 design choice (c).
    # ------------------------------------------------------------------

    @api_command("remote_build/set_pairing_window")
    async def set_pairing_window(
        self,
        *,
        open: bool,  # noqa: A002 — wire format names this field "open"
        client: Hashable,
        **kwargs: Any,
    ) -> PairingWindowState:
        """
        Open, extend, or close the pairing window for the calling client.

        Refcounted per WS client: ``open=true`` adds/refreshes
        the caller's entry, ``open=false`` removes it. Window is
        open iff any client has a non-stale entry. Crashed tabs
        age out via the 5min idle timeout; a graceful close from
        one tab leaves the window open for others.

        ``client`` is the WS connection injected by the
        dispatcher — used as the refcount key so two tabs get
        distinct entries. Required kwarg (a default would
        silently bucket every caller under the same key).

        Fires :attr:`EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED`
        only on real state transitions; idempotent calls don't.
        """
        if not isinstance(open, bool):
            msg = "remote_build/set_pairing_window: 'open' must be a bool"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        was_open = self.is_pairing_window_open()
        if open:
            self.state.pairing_window_clients[client] = time.monotonic()
        else:
            self.state.pairing_window_clients.pop(client, None)
        self._reschedule_pairing_window_close()
        is_open = bool(self.state.pairing_window_clients)

        # Fire on state transitions AND on every extend (so the
        # frontend countdown re-syncs against the bumped deadline).
        if was_open != is_open or (open and is_open):
            self._fire_pairing_window_changed()
        if was_open and not is_open:
            self._clear_pending_peers_on_window_close()
        return self._pairing_window_state()

    def is_pairing_window_open(self) -> bool:
        """Return whether the pairing window is currently open (post-prune)."""
        self._prune_stale_pairing_window_clients()
        return bool(self.state.pairing_window_clients)

    def _pairing_window_remaining(self) -> float | None:
        """Seconds until the latest-extend deadline, or ``None`` if closed."""
        self._prune_stale_pairing_window_clients()
        if not self.state.pairing_window_clients:
            return None
        latest_extend = max(self.state.pairing_window_clients.values())
        return max(0.0, latest_extend + _PAIRING_WINDOW_DURATION_SECONDS - time.monotonic())

    def _pairing_window_state(self) -> PairingWindowState:
        """Project the in-memory client map into a wire-shape response."""
        remaining = self._pairing_window_remaining()
        if remaining is None:
            return PairingWindowState(open=False, expires_in_seconds=None)
        return PairingWindowState(open=True, expires_in_seconds=remaining)

    def _fire_pair_status_changed(
        self, dashboard_id: str, status: Literal["approved", "removed"]
    ) -> None:
        """Fire ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` for a peer transition."""
        payload: RemoteBuildPairStatusChangedData = {
            "dashboard_id": dashboard_id,
            "status": status,
        }
        self._db.bus.fire(EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED, payload)

    def _fire_pairing_window_changed(self) -> None:
        """Fire ``REMOTE_BUILD_PAIRING_WINDOW_CHANGED`` with the current state."""
        state = self._pairing_window_state()
        payload: RemoteBuildPairingWindowChangedData = {
            "open": state.open,
            "expires_in_seconds": state.expires_in_seconds,
        }
        self._db.bus.fire(EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED, payload)

    def _prune_stale_pairing_window_clients(self) -> None:
        """Drop client entries whose last-extend timestamp aged out."""
        if not self.state.pairing_window_clients:
            return
        cutoff = time.monotonic() - _PAIRING_WINDOW_DURATION_SECONDS
        self.state.pairing_window_clients = {
            client: extended_at
            for client, extended_at in self.state.pairing_window_clients.items()
            if extended_at >= cutoff
        }

    def _reschedule_pairing_window_close(self) -> None:
        """
        Cancel any pending close handle and schedule a fresh one.

        Called after every :meth:`set_pairing_window` mutation. The
        handle always reflects the current latest-extend deadline,
        so on every extend we cancel and reschedule rather than
        letting an old handle wake up and re-check; this avoids the
        duplicate-close-event class of bug where an old handle
        would fire after an explicit close.

        When the client map is empty (the explicit-close case where
        the last client just dropped out), no new handle is
        scheduled and ``_pairing_window_handle`` stays ``None``.
        """
        if self.state.pairing_window_handle is not None:
            self.state.pairing_window_handle.cancel()
            self.state.pairing_window_handle = None
        remaining = self._pairing_window_remaining()
        if remaining is None:
            return
        loop = asyncio.get_running_loop()
        self.state.pairing_window_handle = loop.call_later(
            remaining, self._on_pairing_window_deadline
        )

    def _on_pairing_window_deadline(self) -> None:
        """
        Sync callback fired by the TimerHandle when the deadline lapses.

        The handle was scheduled to the latest-extend deadline; if
        any later extend had bumped the deadline, the handle would
        have been cancelled and rescheduled, so by the time we run
        every client has aged out. Clear the client refcount + the
        in-memory PENDING peers dict, fire the close event +
        cancellation events, done.
        """
        self.state.pairing_window_handle = None
        self.state.pairing_window_clients.clear()
        self._fire_pairing_window_changed()
        self._clear_pending_peers_on_window_close()

    def _clear_pending_peers_on_window_close(self) -> None:
        """Drop every PENDING peer + fire ``status="removed"`` for each.

        Wakes any in-flight ``lookup_peer_for_status`` long-poll
        so its offloader sees REJECTED.
        """
        if not self.state.pending_peers:
            return
        cleared = list(self.state.pending_peers)
        self.state.pending_peers.clear()
        for dashboard_id in cleared:
            self._fire_pair_status_changed(dashboard_id, "removed")
