"""
Offloader-side controller for the remote-build feature.

Owns the dashboard's *outbound* role: discovering peer
receivers via mDNS, persisting the per-pin
:class:`StoredPairing` table, driving the pair-request →
pair-status long-poll lifecycle, and keeping one
:class:`PeerLinkClient` per APPROVED pairing alive for
``submit_job`` / ``cancel_job`` / ``download_artifacts`` to
reach through.

Pairs with :class:`~.receiver.ReceiverController` — the two
siblings own disjoint state and never reach across; the only
shared coupling is :mod:`._shared` (a free
:func:`drain_tasks` helper) and the
:class:`~esphome_device_builder.device_builder.DeviceBuilder`
reference passed to both at construction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from esphome import const as _esphome_const
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from ...helpers.api import api_command
from ...helpers.build_scheduler import BuildSchedulerInputs
from ...helpers.dashboard_identity import get_or_create_identities
from ...helpers.event_bus import Event
from ...helpers.peer_link_resolver import make_peer_link_resolver
from ...helpers.storage import Store
from ...models import (
    EventType,
    OffloaderAlertSnapshotEntry,
    OffloaderJobStateChangedData,
    OffloaderPairAlertDismissedData,
    OffloaderPairPeerRevokedData,
    OffloaderPairPinMismatchData,
    OffloaderPeerLinkClosedData,
    OffloaderPeerLinkOpenedData,
    OffloaderQueueStatusChangedData,
    OffloaderRemoteBuildSettings,
    OffloaderRemoteBuildSettingsView,
    OffloaderRemoteJobSnapshotEntry,
    OffloaderSettingsSnapshot,
    PairingSummary,
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    RemoteBuildIdentityRotatedData,
    RemoteBuildPeer,
    StoredPairing,
)
from . import (
    bus_handlers,
    discovery,
    pair_commands,
    pair_status,
    peer_link_lifecycle,
    rebind,
    settings_commands,
    submit_job_commands,
)
from ._models import RebindProbeResult
from ._shared import _RemoteBuildBase, drain_tasks
from ._state import OffloaderState
from ._storage_codecs import (
    OFFLOADER_PAIRINGS_FILE,
    decode_pairings,
    encode_pairings,
)
from ._summaries import pairing_summary
from .peer_link_client import PairStatusResult

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.dashboard_identity import DashboardIdentity
    from ...helpers.peer_link_identity import PeerLinkIdentity
    from .peer_link_client import PeerLinkClient

_LOGGER = logging.getLogger(__name__)


# Debounce window for the offloader-side pairings-store write
# so a burst of approvals collapses to one disk write.
_PAIRINGS_SAVE_DELAY_SECONDS = 1.0


class OffloaderController(_RemoteBuildBase):  # noqa: PLR0904
    """Outbound side of remote-build: pair, peer-link, submit/cancel/download."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        super().__init__(device_builder)
        self.state = OffloaderState()
        self._pairings_store: Store[OffloaderRemoteBuildSettings] = Store(
            self._db.settings.config_dir / OFFLOADER_PAIRINGS_FILE,
            encoder=encode_pairings,
            decoder=decode_pairings,
            shutdown_register=self._shutdown_callbacks.append,
            name="offloader_pairings",
        )

    async def start(self) -> None:
        """Seed pairings from disk, cache identities, spawn peer-link clients."""
        state = self.state
        if (settings := await self._pairings_store.async_load()) is not None:
            for pairing in settings.pairings:
                state.pairings[pairing.pin_sha256] = pairing
            state.remote_builds_enabled = settings.remote_builds_enabled
            state.version_match_policy = settings.version_match_policy
            state.include_local_in_pool = settings.include_local_in_pool
        # Populates ``state.offloader_peer_link_priv`` /
        # ``offloader_dashboard_id`` so the spawns below see them.
        await self._load_offloader_identities_async()
        # Wire the resolver before spawning clients so each picks
        # it up at construction; stays None when zeroconf is down
        # and outbound connects fall back to the OS resolver.
        self._setup_peer_link_resolver()
        for pairing in state.pairings.values():
            if pairing.status is PeerStatus.APPROVED:
                self._spawn_peer_link_client(pairing)
        self._subscribe(
            EventType.OFFLOADER_QUEUE_STATUS_CHANGED, self._on_offloader_queue_status_changed
        )
        self._subscribe(EventType.OFFLOADER_JOB_STATE_CHANGED, self._on_offloader_job_state_changed)
        self._subscribe(EventType.OFFLOADER_PAIR_PIN_MISMATCH, self._on_offloader_pair_pin_mismatch)
        self._subscribe(EventType.OFFLOADER_PAIR_PEER_REVOKED, self._on_offloader_pair_peer_revoked)
        self._subscribe(EventType.OFFLOADER_PEER_LINK_OPENED, self._on_offloader_peer_link_opened)
        self._subscribe(EventType.OFFLOADER_PEER_LINK_CLOSED, self._on_offloader_peer_link_closed)
        self._subscribe(
            EventType.REMOTE_BUILD_IDENTITY_ROTATED, self._on_remote_build_identity_rotated
        )
        self._start_discovery()

    async def stop(self) -> None:
        """Cancel the browser, drain tasks, flush store, clear dicts."""
        state = self.state
        if state.browser is not None:
            try:
                await state.browser.async_cancel()
            except Exception:
                _LOGGER.debug("remote-build browser cancel failed", exc_info=True)
            state.browser = None
        self._listeners.close()
        await drain_tasks(self._tasks)
        self._tasks.clear()
        await drain_tasks(state.pair_status_listeners.values())
        state.pair_status_listeners.clear()
        # Each peer-link client's CancelledError handler sends a
        # ``client_stopped`` terminate so the receiver doesn't wait
        # on its heartbeat to time out.
        await drain_tasks(h.task for h in state.peer_link_clients.values())
        state.peer_link_clients.clear()
        for callback in self._shutdown_callbacks:
            await callback()
        state.pairings.clear()
        state.peer_queue_status.clear()
        state.offloader_remote_jobs.clear()
        state.open_peer_links.clear()
        state.rebind_probe_until.clear()
        state.peers.clear()
        await self._close_peer_link_resolver()

    async def _load_offloader_identities_async(
        self,
    ) -> tuple[PeerLinkIdentity, DashboardIdentity]:
        """Load both offloader identities and refresh the cached ``state`` snapshot."""
        peer_link_identity, dashboard_identity = await get_or_create_identities(
            self._db.settings.config_dir,
            self._db.peer_link_identity_store,
        )
        self.state.offloader_peer_link_priv = peer_link_identity.private_bytes
        self.state.offloader_dashboard_id = dashboard_identity.dashboard_id
        return peer_link_identity, dashboard_identity

    def _setup_peer_link_resolver(self) -> None:
        """
        Build the shared mDNS-aware aiohttp resolver if zeroconf is up.

        Reads the same ``self._db.devices.zeroconf`` reference
        the discovery browser uses, so resolver-availability
        and browser-availability are bound together — either
        both run or both stay off. Stores the resolver on
        :attr:`_peer_link_resolver`; leaves it ``None`` when
        the shared zeroconf isn't available (devices controller
        not constructed, monitor failed to bind, HA-addon mode
        without zeroconf) **or** when the resolver constructor
        itself raises (e.g. the upstream
        :class:`aiohttp.resolver.AsyncResolver` ``__init__``
        raises ``RuntimeError`` when ``aiodns`` isn't installed,
        which can happen in lean env paths that drop the
        transitive dep). Fail-soft: the next ``aiohttp`` connect
        falls back to the OS resolver in either case, same
        contract as :meth:`_start_discovery`.
        """
        if self._db.devices is None:
            return
        zeroconf = self._db.devices.zeroconf
        if zeroconf is None:
            return
        try:
            self.state.peer_link_resolver = make_peer_link_resolver(zeroconf)
        except Exception:
            _LOGGER.exception(
                "Could not build peer-link mDNS resolver; outbound peer-link connects "
                "will fall back to the OS resolver"
            )
            self.state.peer_link_resolver = None

    def _start_discovery(self) -> None:
        """Bring up the mDNS service browser for peer discovery."""
        discovery.start_discovery(self)

    def _on_offloader_pair_pin_mismatch(self, event: Event[OffloaderPairPinMismatchData]) -> None:
        """Cache the alert in ``_offloader_alerts`` for late-subscriber snapshot."""
        bus_handlers.on_offloader_pair_pin_mismatch(self, event)

    def _on_offloader_pair_peer_revoked(self, event: Event[OffloaderPairPeerRevokedData]) -> None:
        """Cache the peer-revoked alert in ``_offloader_alerts`` for late-subscriber snapshot."""
        bus_handlers.on_offloader_pair_peer_revoked(self, event)

    def _on_offloader_peer_link_opened(self, event: Event[OffloaderPeerLinkOpenedData]) -> None:
        """Add ``pin_sha256`` to ``_open_peer_links`` and refresh the receiver version."""
        bus_handlers.on_offloader_peer_link_opened(self, event)

    def _on_offloader_peer_link_closed(self, event: Event[OffloaderPeerLinkClosedData]) -> None:
        """Discard ``pin_sha256`` from ``_open_peer_links`` on session close."""
        bus_handlers.on_offloader_peer_link_closed(self, event)

    def _on_remote_build_identity_rotated(
        self, event: Event[RemoteBuildIdentityRotatedData]
    ) -> None:
        """Refresh the cached identity + respawn live peer-link clients on rotation."""
        self._track_task(
            self._refresh_identity_and_respawn_clients(), name="offloader-identity-refresh"
        )

    async def _refresh_identity_and_respawn_clients(self) -> None:
        """Reload the identity snapshot and respawn every APPROVED peer-link client."""
        await self._load_offloader_identities_async()
        for pin_sha256 in list(self.state.peer_link_clients):
            pairing = self.state.pairings.get(pin_sha256)
            if pairing is None or pairing.status is not PeerStatus.APPROVED:
                continue
            self._cancel_peer_link_client(pin_sha256)
            self._spawn_peer_link_client(pairing)

    def _on_offloader_queue_status_changed(
        self, event: Event[OffloaderQueueStatusChangedData]
    ) -> None:
        """Update the offloader-side ``_peer_queue_status`` cache from a wire event."""
        bus_handlers.on_offloader_queue_status_changed(self, event)

    def peer_queue_status_snapshot(self) -> list[PeerQueueStatusSnapshotEntry]:
        """Per-peer queue-status snapshot for ``subscribe_events`` seeding."""
        return list(self.state.peer_queue_status.values())

    def _on_offloader_job_state_changed(self, event: Event[OffloaderJobStateChangedData]) -> None:
        """Maintain the offloader-side in-flight remote-job cache."""
        bus_handlers.on_offloader_job_state_changed(self, event)

    def offloader_remote_jobs_snapshot(self) -> list[OffloaderRemoteJobSnapshotEntry]:
        """In-flight remote-job snapshot for ``subscribe_events`` seeding."""
        return list(self.state.offloader_remote_jobs.values())

    async def _close_peer_link_resolver(self) -> None:
        """Release the shared mDNS-aware aiohttp resolver, if any.

        Idempotent. The borrowed :class:`AsyncZeroconf` is
        closed separately by the device-state monitor.
        """
        if self.state.peer_link_resolver is None:
            return
        try:
            await self.state.peer_link_resolver.real_close()
        except Exception:
            _LOGGER.debug("peer-link resolver close failed", exc_info=True)
        self.state.peer_link_resolver = None

    # ------------------------------------------------------------------
    # mDNS plumbing
    # ------------------------------------------------------------------

    def _on_service_state_change(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Browser callback; resolve the service info and update the peer map."""
        discovery.on_service_state_change(self, zeroconf, service_type, name, state_change)

    async def _resolve_and_apply(self, zeroconf: Any, info: AsyncServiceInfo, name: str) -> None:
        """Async resolve path for cache misses."""
        await discovery.resolve_and_apply(self, zeroconf, info, name)

    def _upsert_host(self, name: str, info: AsyncServiceInfo) -> None:
        """Replace the row keyed on *name* and fire ``REMOTE_BUILD_HOST_ADDED``."""
        discovery.upsert_host(self, name, info)

    def _is_self_endpoint(self, hostname: str, port: int) -> bool:
        """Return True when *(hostname, port)* matches our published advertise."""
        return discovery.is_self_endpoint(self, hostname, port)

    def _fire_host_removed(self, name: str) -> None:
        """Fire ``REMOTE_BUILD_HOST_REMOVED`` for *name*."""
        discovery.fire_host_removed(self, name)

    def _schedule_pairings_save(self) -> None:
        """Debounce-write the offloader pairings store via the per-file Store."""
        self._pairings_store.async_delay_save(
            self._serialize_pairings, delay=_PAIRINGS_SAVE_DELAY_SECONDS
        )

    async def _probe_pairing_endpoint(
        self,
        *,
        pairing: StoredPairing,
        new_hostname: str,
        new_port: int,
    ) -> RebindProbeResult:
        """Probe + identity-verify a candidate endpoint without mutating state."""
        return await rebind.probe_pairing_endpoint(
            self, pairing=pairing, new_hostname=new_hostname, new_port=new_port
        )

    def _commit_endpoint_rebind(self, pairing: StoredPairing, *, hostname: str, port: int) -> None:
        """Mutate *pairing* to (*hostname*, *port*) and run the rebind epilogue."""
        rebind.commit_endpoint_rebind(self, pairing, hostname=hostname, port=port)

    # ------------------------------------------------------------------
    # mDNS auto-rebind
    # ------------------------------------------------------------------

    def _maybe_schedule_rebind_probe(self, peer: RemoteBuildPeer) -> None:
        """Spawn a probe-and-rebind task if *peer* is a known pin at a new endpoint."""
        rebind.maybe_schedule_rebind_probe(self, peer)

    async def _probe_and_rebind_endpoint(
        self, *, pairing: StoredPairing, new_hostname: str, new_port: int
    ) -> None:
        """Probe the candidate endpoint; rebind the pairing iff the pin still matches."""
        await rebind.probe_and_rebind_endpoint(
            self, pairing=pairing, new_hostname=new_hostname, new_port=new_port
        )

    def hosts_snapshot(self) -> list[RemoteBuildPeer]:
        """Return the current mDNS-discovered hosts for ``subscribe_events`` seeding."""
        return list(self.state.peers.values())

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------

    @api_command("remote_build/get_offloader_settings")
    async def get_offloader_settings(self, **kwargs: Any) -> OffloaderRemoteBuildSettingsView:
        """Return the offloader-side settings view (master toggle + pairings list)."""
        return await settings_commands.get_offloader_settings(self)

    @api_command("remote_build/set_offloader_settings")
    async def set_offloader_settings(
        self,
        *,
        remote_builds_enabled: bool | None = None,
        version_match_policy: str | None = None,
        include_local_in_pool: bool | None = None,
        **kwargs: Any,
    ) -> OffloaderRemoteBuildSettingsView:
        """Flip the offloader-side master settings for transparent install."""
        return await settings_commands.set_offloader_settings(
            self,
            remote_builds_enabled=remote_builds_enabled,
            version_match_policy=version_match_policy,
            include_local_in_pool=include_local_in_pool,
        )

    @api_command("remote_build/set_pairing_enabled")
    async def set_pairing_enabled(
        self,
        *,
        pin_sha256: str,
        enabled: bool,
        **kwargs: Any,
    ) -> PairingSummary:
        """Flip the per-pairing enable switch for transparent install."""
        return await pair_commands.set_pairing_enabled(self, pin_sha256=pin_sha256, enabled=enabled)

    @api_command("remote_build/preview_pair")
    async def preview_pair(self, *, hostname: str, port: int, **kwargs: Any) -> dict[str, str]:
        """Open a brief Noise XX WS to *hostname*:*port* and return the receiver's pin."""
        return await pair_commands.preview_pair(self, hostname=hostname, port=port)

    @api_command("remote_build/request_pair")
    async def request_pair(
        self,
        *,
        hostname: str,
        port: int,
        pin_sha256: str,
        receiver_label: str,
        offloader_label: str,
        **kwargs: Any,
    ) -> PairingSummary:
        """Open a Noise XX WS, send ``intent="pair_request"``, persist a local row."""
        return await pair_commands.request_pair(
            self,
            hostname=hostname,
            port=port,
            pin_sha256=pin_sha256,
            receiver_label=receiver_label,
            offloader_label=offloader_label,
        )

    @api_command("remote_build/unpair")
    async def unpair(
        self,
        *,
        pin_sha256: str,
        **kwargs: Any,
    ) -> dict[str, bool]:
        """Drop the local :class:`StoredPairing` row keyed on *pin_sha256*."""
        return await pair_commands.unpair(self, pin_sha256=pin_sha256)

    @api_command("remote_build/edit_pairing_endpoint")
    async def edit_pairing_endpoint(
        self,
        *,
        pin_sha256: str,
        hostname: str,
        port: int,
        **kwargs: Any,
    ) -> PairingSummary:
        """Manually rebind *pin_sha256*'s pairing onto new (*hostname*, *port*) coords."""
        return await pair_commands.edit_pairing_endpoint(
            self, pin_sha256=pin_sha256, hostname=hostname, port=port
        )

    async def _validate_submit_job_config(self, configuration: object) -> tuple[str, Path]:
        """Validate the WS *configuration* arg, return ``(name, yaml_path)``."""
        return await submit_job_commands.validate_submit_job_config(self, configuration)

    def _lookup_open_peer_link_client(self, pin_sha256: str, *, label: str) -> PeerLinkClient:
        """Return the live :class:`PeerLinkClient` for *pin_sha256*, raising on miss."""
        return peer_link_lifecycle.lookup_open_peer_link_client(self, pin_sha256, label=label)

    async def _build_submit_job_bundle(self, configuration: str, yaml_path: Path) -> bytes:
        """Build the bundle bytes for *yaml_path*."""
        return await submit_job_commands.build_submit_job_bundle(self, configuration, yaml_path)

    @api_command("remote_build/submit_job")
    async def submit_job(
        self,
        *,
        pin_sha256: str,
        configuration: str,
        target: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Bundle *configuration* and dispatch a build to the receiver behind *pin_sha256*."""
        return await submit_job_commands.submit_job(
            self, pin_sha256=pin_sha256, configuration=configuration, target=target
        )

    @api_command("remote_build/download_artifacts")
    async def download_artifacts(
        self,
        *,
        pin_sha256: str,
        job_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Fetch the build's flash-artifact set for *job_id* from the paired receiver."""
        return await submit_job_commands.download_artifacts(
            self, pin_sha256=pin_sha256, job_id=job_id
        )

    @api_command("remote_build/cancel_job")
    async def cancel_job(
        self,
        *,
        pin_sha256: str,
        job_id: str,
        **kwargs: Any,
    ) -> dict[str, bool]:
        """Send a ``cancel_job`` frame to the receiver behind *pin_sha256*."""
        return await submit_job_commands.cancel_job(self, pin_sha256=pin_sha256, job_id=job_id)

    def get_pairing(self, pin_sha256: str) -> StoredPairing | None:
        """Return the :class:`StoredPairing` for *pin_sha256*, or ``None``."""
        return self.state.pairings.get(pin_sha256)

    def offloader_settings_snapshot(self) -> OffloaderSettingsSnapshot:
        """Bundle the offloader-wide settings for the initial-state seed."""
        return {
            "remote_builds_enabled": self.state.remote_builds_enabled,
            "version_match_policy": self.state.version_match_policy,
            "include_local_in_pool": self.state.include_local_in_pool,
        }

    def build_scheduler_snapshot(self) -> BuildSchedulerInputs:
        """Bundle the scheduler's input state into a shallow immutable snapshot.

        Shallow only — :class:`StoredPairing` rows mutate in
        place elsewhere, but the scheduler runs sync on the
        same loop tick.
        """
        return BuildSchedulerInputs(
            remote_builds_enabled=self.state.remote_builds_enabled,
            pairings=dict(self.state.pairings),
            open_peer_links=frozenset(self.state.open_peer_links),
            peer_queue_status=dict(self.state.peer_queue_status),
            offloader_esphome_version=_esphome_const.__version__,
            version_match_policy=self.state.version_match_policy,
            include_local_in_pool=self.state.include_local_in_pool,
        )

    def pairings_snapshot(self) -> list[PairingSummary]:
        """Return the in-memory pairings (PENDING + APPROVED) for ``subscribe_events`` seeding."""
        return [self._pairing_summary_for(p) for p in self.state.pairings.values()]

    def _pairing_summary_for(self, pairing: StoredPairing) -> PairingSummary:
        """Project *pairing* into a wire :class:`PairingSummary`.

        Threads the live ``connected`` / ``connecting`` /
        ``last_connect_error`` state off the matching peer-link
        client handle (if any), so the snapshot path and the
        per-mutation ``_apply_pair_status_result`` response use
        one source of truth and can't drift on the dynamic
        connection-state fields. PENDING rows have no client
        handle in :attr:`_peer_link_clients` (the offloader only
        spawns a client when the receiver flips the row to
        APPROVED), so they fall through the ``handle is None``
        branch with all three fields at their connection-quiet
        defaults.
        """
        handle = self.state.peer_link_clients.get(pairing.pin_sha256)
        return pairing_summary(
            pairing,
            connected=pairing.pin_sha256 in self.state.open_peer_links,
            connecting=handle is not None and handle.client.is_connecting,
            last_connect_error=(handle.client.last_connect_error if handle is not None else ""),
        )

    def offloader_alerts_snapshot(self) -> list[OffloaderAlertSnapshotEntry]:
        """Offloader alerts (insertion-ordered, newest last) for ``subscribe_events`` seeding."""
        return list(self.state.offloader_alerts.values())

    def _dismiss_offloader_alert(self, pin_sha256: str, hostname: str, port: int) -> bool:
        """Drop the alert for *pin_sha256* and fire DISMISSED. Returns whether a row was dropped.

        Called only by ``request_pair`` (re-pair → stale alert)
        and ``unpair`` (row gone → alert moot). No operator-driven
        dismiss surface — re-pair / unpair are the only resolutions.
        """
        if self.state.offloader_alerts.pop(pin_sha256, None) is None:
            return False
        payload: OffloaderPairAlertDismissedData = {
            "receiver_hostname": hostname,
            "receiver_port": port,
            "pin_sha256": pin_sha256,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_ALERT_DISMISSED, payload)
        return True

    def _serialize_pairings(self) -> OffloaderRemoteBuildSettings:
        """Build the on-disk pairings shape from RAM, dropping PENDING rows."""
        return OffloaderRemoteBuildSettings(
            pairings=[p for p in self.state.pairings.values() if p.status is PeerStatus.APPROVED],
            remote_builds_enabled=self.state.remote_builds_enabled,
            version_match_policy=self.state.version_match_policy,
            include_local_in_pool=self.state.include_local_in_pool,
        )

    # ------------------------------------------------------------------
    # Pair-status listeners — one task per PENDING StoredPairing, each
    # holding an open Noise WS to its receiver with
    # ``intent="pair_status"``. Receiver-side responder waits on
    # its own bus event for an admin click and pushes the response
    # back, so the offloader sees the flip with sub-second latency
    # without polling.
    # ------------------------------------------------------------------

    def _spawn_pair_status_listener(self, pairing: StoredPairing) -> None:
        """Spawn the pair-status listener task for *pairing* if not already running."""
        pair_status.spawn_pair_status_listener(self, pairing)

    def _cancel_pair_status_listener(self, pin_sha256: str) -> None:
        """Cancel the listener for *pin_sha256*. No-op if none running."""
        pair_status.cancel_pair_status_listener(self, pin_sha256)

    def _spawn_peer_link_client(self, pairing: StoredPairing) -> None:
        """Spawn the long-lived peer-link client for *pairing*."""
        peer_link_lifecycle.spawn_peer_link_client(self, pairing)

    def _cancel_peer_link_client(self, pin_sha256: str) -> None:
        """Cancel the peer-link client for *pin_sha256*. No-op if none running."""
        peer_link_lifecycle.cancel_peer_link_client(self, pin_sha256)

    def _sweep_stale_pairings_at_endpoint(
        self, hostname: str, port: int, *, keep_pin_sha256: str
    ) -> None:
        """Drop any pairing or alert at ``(hostname, port)`` whose pin isn't *keep_pin_sha256*."""
        peer_link_lifecycle.sweep_stale_pairings_at_endpoint(
            self, hostname, port, keep_pin_sha256=keep_pin_sha256
        )

    async def _await_pair_status_flip(self, pairing: StoredPairing) -> None:
        """Hold a Noise WS to the receiver until the row flips status."""
        await pair_status.await_pair_status_flip(self, pairing)

    async def _apply_pair_status_result(
        self, pairing: StoredPairing, result: PairStatusResult
    ) -> bool:
        """Apply a pair-status response. Return True when the listener should exit."""
        return await pair_status.apply_pair_status_result(self, pairing, result)

    def _fire_offloader_pair_status_changed(
        self,
        receiver_hostname: str,
        receiver_port: int,
        pin_sha256: str,
        status: Literal["approved", "removed"],
    ) -> None:
        """Fire ``OFFLOADER_PAIR_STATUS_CHANGED`` for a pairing flip."""
        pair_status.fire_offloader_pair_status_changed(
            self, receiver_hostname, receiver_port, pin_sha256, status
        )

    def _fire_offloader_pairing_added(self, summary: PairingSummary) -> None:
        """Fire ``OFFLOADER_PAIRING_ADDED`` so other tabs build the new row."""
        pair_status.fire_offloader_pairing_added(self, summary)

    # ------------------------------------------------------------------
    # Identity — surface the receiver's own dashboard_id + cert pin to
    # the Settings UI without making it reach into the cert PEM
    # directly. Rotation lives next door so the "rotate" button can
    # land in the same controller.
    # ------------------------------------------------------------------
