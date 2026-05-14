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
    RemoteBuildSettings,
    RemoteBuildSettingsView,
)
from ..config import (
    load_remote_build_settings,
)
from . import (
    cleanup_loop,
    identity_commands,
    pair_flow,
    peer_crud,
    peer_link_sessions,
    settings_receiver,
)
from ._receiver_state import ReceiverState
from ._shared import _RemoteBuildBase, drain_tasks
from ._storage_codecs import (
    RECEIVER_PEERS_FILE,
    decode_peers,
    encode_peers,
)
from ._summaries import peer_summary
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
        return await settings_receiver.get_settings(self)

    def _to_view(self, settings: RemoteBuildSettings) -> RemoteBuildSettingsView:
        """Project receiver settings to wire view, merging in-memory peers."""
        return settings_receiver.to_view(self, settings)

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
        """Run *mutator* against the current settings and persist the result."""
        return await settings_receiver.modify_settings(self, mutator)

    @api_command("remote_build/set_settings")
    async def set_settings(
        self,
        *,
        enabled: bool,
        cleanup_ttl_seconds: int | None = None,
        **kwargs: Any,
    ) -> RemoteBuildSettingsView:
        """Persist the receiver-side ``enabled`` master switch."""
        return await settings_receiver.set_settings(
            self, enabled=enabled, cleanup_ttl_seconds=cleanup_ttl_seconds
        )

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
        """Promote a PENDING peer to APPROVED."""
        return await peer_crud.approve_peer(self, dashboard_id=dashboard_id)

    @api_command("remote_build/remove_peer")
    async def remove_peer(self, *, dashboard_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """Delete a peer row (works on both PENDING and APPROVED)."""
        return await peer_crud.remove_peer(self, dashboard_id=dashboard_id)

    def _serialize_peers(self) -> ReceiverPeers:
        """Build the on-disk peers shape from the in-RAM ``state.approved_peers`` dict."""
        return ReceiverPeers(peers=list(self.state.approved_peers.values()))

    async def _current_settings_view(self) -> RemoteBuildSettingsView:
        """Load settings from disk and project to the wire view (post-mutation response)."""
        return await settings_receiver.current_settings_view(self)

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
        """Process an ``intent="pair_request"`` Noise session."""
        return await pair_flow.record_pair_request(
            self,
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            static_x25519_pub=static_x25519_pub,
            label=label,
            peer_ip=peer_ip,
        )

    async def lookup_peer_for_session(
        self, *, dashboard_id: str, pin_sha256: str
    ) -> IntentResponse:
        """Resolve an ``intent="peer_link"`` request."""
        return await pair_flow.lookup_peer_for_session(
            self, dashboard_id=dashboard_id, pin_sha256=pin_sha256
        )

    async def lookup_peer_for_status(self, *, dashboard_id: str, pin_sha256: str) -> IntentResponse:
        """Resolve an ``intent="pair_status"`` query, long-polling on PENDING."""
        return await pair_flow.lookup_peer_for_status(
            self, dashboard_id=dashboard_id, pin_sha256=pin_sha256
        )

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
        pair_flow.fire_pair_status_changed(self, dashboard_id, status)

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
