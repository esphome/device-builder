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

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ...helpers.api import CommandError, api_command
from ...helpers.build_scheduler import BuildSchedulerInputs
from ...helpers.dashboard_identity import get_or_create_identity
from ...helpers.event_bus import Event
from ...helpers.peer_link_identity import get_or_create_peer_link_identity
from ...helpers.peer_link_resolver import PeerLinkDNSResolver, make_peer_link_resolver
from ...helpers.storage import Store
from ...models import (
    PAIRING_VERSION_MAX_LEN,
    ErrorCode,
    EventType,
    IntentResponse,
    OffloaderAlertSnapshotEntry,
    OffloaderJobStateChangedData,
    OffloaderPairAlertDismissedData,
    OffloaderPairingEnabledChangedData,
    OffloaderPairPeerRevokedData,
    OffloaderPairPinMismatchData,
    OffloaderPairStatusChangedData,
    OffloaderPeerLinkClosedData,
    OffloaderPeerLinkOpenedData,
    OffloaderPeerRevokedAlert,
    OffloaderPinMismatchAlert,
    OffloaderQueueStatusChangedData,
    OffloaderRemoteBuildSettings,
    OffloaderRemoteBuildSettingsView,
    OffloaderRemoteBuildsToggledData,
    OffloaderRemoteJobSnapshotEntry,
    PairingSummary,
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    RemoteBuildPeer,
    StoredPairing,
)
from . import discovery, peer_link_lifecycle, rebind
from ._mdns import endpoints_equal
from ._models import (
    EDIT_PAIRING_PROBE_ERRORS,
    RebindProbeOutcome,
    RebindProbeResult,
)
from ._shared import _RemoteBuildBase, drain_tasks
from ._storage_codecs import (
    OFFLOADER_PAIRINGS_FILE,
    decode_pairings,
    encode_pairings,
)
from ._summaries import pairing_summary
from ._validators import (
    HostFieldContext,
    PairLabelField,
    download_artifacts_error_to_command_error,
    enforce_pin_match,
    intent_response_to_command_error,
    validate_bool,
    validate_hostname,
    validate_pair_label,
    validate_pin_sha256,
    validate_port,
    validate_submit_job_target,
)
from .artifacts_tarball import UnpackArtifactsError, unpack_artifacts_response
from .peer_link_client import (
    DownloadArtifactsError,
    PairStatusResult,
    PeerLinkClientError,
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
)
from .peer_link_client import (
    await_pair_status as peer_link_await_pair_status,
)
from .peer_link_client import (
    preview_pair as peer_link_preview_pair,
)
from .peer_link_client import (
    request_pair as peer_link_request_pair,
)

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.dashboard_identity import DashboardIdentity
    from ...helpers.peer_link_identity import PeerLinkIdentity
    from ._models import PeerLinkClientHandle
    from .peer_link_client import PeerLinkClient

_LOGGER = logging.getLogger(__name__)


def _load_offloader_identities(
    config_dir: Path,
) -> tuple[PeerLinkIdentity, DashboardIdentity]:
    """Load both offloader-side identities in one executor hop.

    Bundling keeps the async caller's body to a single
    ``await`` instead of two.
    """
    return get_or_create_peer_link_identity(config_dir), get_or_create_identity(config_dir)


# Terminal status set for the offloader-side remote-job cache
# drop-on-terminal logic.
_OFFLOADER_REMOTE_JOB_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)

# Reconnect backoff for a pair-status listener whose Noise WS
# died on transport error — bounds tight-looping against a
# hard-down receiver.
_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS = 2.0

# Debounce window for the offloader-side pairings-store write
# so a burst of approvals collapses to one disk write.
_PAIRINGS_SAVE_DELAY_SECONDS = 1.0


class OffloaderController(_RemoteBuildBase):  # noqa: PLR0904
    """Outbound side of remote-build: pair, peer-link, submit/cancel/download."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        super().__init__(device_builder)
        self._browser: AsyncServiceBrowser | None = None
        self._peer_link_resolver: PeerLinkDNSResolver | None = None
        self._peers: dict[str, RemoteBuildPeer] = {}
        self._rebind_probe_until: dict[str, float] = {}
        self._own_instance_name: str | None = None
        self._pair_status_listeners: dict[str, asyncio.Task[None]] = {}
        self._peer_link_clients: dict[str, PeerLinkClientHandle] = {}
        self._open_peer_links: set[str] = set()
        # Cached at :meth:`start`; WS-command handlers re-read
        # from disk via :meth:`_load_offloader_identities_async`
        # to pick up rotations.
        self._offloader_dashboard_id: str | None = None
        self._offloader_peer_link_priv: bytes | None = None
        self._pairings: dict[str, StoredPairing] = {}
        self._remote_builds_enabled: bool = True
        self._offloader_alerts: dict[str, OffloaderAlertSnapshotEntry] = {}
        self._peer_queue_status: dict[str, PeerQueueStatusSnapshotEntry] = {}
        self._offloader_remote_jobs: dict[str, OffloaderRemoteJobSnapshotEntry] = {}
        self._pairings_store: Store[OffloaderRemoteBuildSettings] = Store(
            self._db.settings.config_dir / OFFLOADER_PAIRINGS_FILE,
            encoder=encode_pairings,
            decoder=decode_pairings,
            shutdown_register=self._shutdown_callbacks.append,
            name="offloader_pairings",
        )

    async def start(self) -> None:
        """Seed pairings from disk, cache identities, spawn peer-link clients."""
        if (settings := await self._pairings_store.async_load()) is not None:
            for pairing in settings.pairings:
                self._pairings[pairing.pin_sha256] = pairing
            self._remote_builds_enabled = settings.remote_builds_enabled
        peer_link_identity, dashboard_identity = await self._load_offloader_identities_async()
        self._offloader_peer_link_priv = peer_link_identity.private_bytes
        self._offloader_dashboard_id = dashboard_identity.dashboard_id
        # Wire the resolver before spawning clients so each picks
        # it up at construction; stays None when zeroconf is down
        # and outbound connects fall back to the OS resolver.
        self._setup_peer_link_resolver()
        for pairing in self._pairings.values():
            if pairing.status is PeerStatus.APPROVED:
                self._spawn_peer_link_client(pairing)
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_QUEUE_STATUS_CHANGED,
                self._on_offloader_queue_status_changed,
            )
        )
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_JOB_STATE_CHANGED,
                self._on_offloader_job_state_changed,
            )
        )
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_PAIR_PIN_MISMATCH,
                self._on_offloader_pair_pin_mismatch,
            )
        )
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_PEER_LINK_OPENED,
                self._on_offloader_peer_link_opened,
            )
        )
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_PEER_LINK_CLOSED,
                self._on_offloader_peer_link_closed,
            )
        )
        self._start_discovery()

    async def stop(self) -> None:
        """Cancel the browser, drain tasks, flush store, clear dicts."""
        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception:
                _LOGGER.debug("remote-build browser cancel failed", exc_info=True)
            self._browser = None
        self._listeners.close()
        await drain_tasks(self._tasks)
        self._tasks.clear()
        await drain_tasks(self._pair_status_listeners.values())
        self._pair_status_listeners.clear()
        # Each peer-link client's CancelledError handler sends a
        # ``client_stopped`` terminate so the receiver doesn't wait
        # on its heartbeat to time out.
        await drain_tasks(h.task for h in self._peer_link_clients.values())
        self._peer_link_clients.clear()
        for callback in self._shutdown_callbacks:
            await callback()
        self._pairings.clear()
        self._peer_queue_status.clear()
        self._offloader_remote_jobs.clear()
        self._open_peer_links.clear()
        self._rebind_probe_until.clear()
        self._peers.clear()
        await self._close_peer_link_resolver()

    async def _load_offloader_identities_async(
        self,
    ) -> tuple[PeerLinkIdentity, DashboardIdentity]:
        """Read both offloader-side identities off the executor.

        WS-command handlers and the pair-status listener
        deliberately re-read from disk on every call rather than
        using the :meth:`start`-time cache on
        :attr:`_offloader_peer_link_priv` /
        :attr:`_offloader_dashboard_id` — :meth:`rotate_identity`
        rewrites the keypair file without updating the cache,
        so caching would sign post-rotation calls with the old
        key.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _load_offloader_identities, self._db.settings.config_dir
        )

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
            self._peer_link_resolver = make_peer_link_resolver(zeroconf)
        except Exception:
            _LOGGER.exception(
                "Could not build peer-link mDNS resolver; outbound peer-link connects "
                "will fall back to the OS resolver"
            )
            self._peer_link_resolver = None

    def _start_discovery(self) -> None:
        """Bring up the mDNS service browser for peer discovery."""
        discovery.start_discovery(self)

    def _on_offloader_pair_pin_mismatch(self, event: Event[OffloaderPairPinMismatchData]) -> None:
        """Cache the alert in ``_offloader_alerts`` for late-subscriber snapshot.

        Keyed on ``pin_sha256`` (matches the synchronous
        mutation site in :meth:`_apply_pair_status_result`).
        The alert payload adds ``kind`` + ``fired_at`` to the
        bus event's wire fields so the snapshot row survives
        the event drop.
        """
        data = event.data
        # Build the typed alert explicitly rather than as a bare
        # dict literal: ``_offloader_alerts`` is typed
        # ``dict[..., OffloaderAlertSnapshotEntry]`` (a union of
        # ``OffloaderPinMismatchAlert`` / ``OffloaderPeerRevokedAlert``
        # discriminated by ``kind``), and a bare literal under
        # strict mypy can fall back to ``dict[str, object]``
        # rather than narrowing into the right TypedDict variant.
        alert: OffloaderPinMismatchAlert = {
            "kind": "pin_mismatch",
            "receiver_hostname": data["receiver_hostname"],
            "receiver_port": data["receiver_port"],
            "pin_sha256": data["pin_sha256"],
            "receiver_label": data["receiver_label"],
            "expected_pin": data["expected_pin"],
            "observed_pin": data["observed_pin"],
            "fired_at": time.time(),
        }
        self._offloader_alerts[data["pin_sha256"]] = alert

    def _on_offloader_peer_link_opened(self, event: Event[OffloaderPeerLinkOpenedData]) -> None:
        """Add ``pin_sha256`` to ``_open_peer_links`` and refresh the receiver version.

        Receiver's ``esphome_version`` rides on every
        ``intent_response`` so a receiver upgrade picks up on
        next session-open without operator action.
        ``pick_build_path``'s deferred version-compat gate reads
        this field.

        Empty / oversize versions are dropped silently rather
        than clobbering — empty would lose the captured value
        after a reconnect from a pre-feature receiver; oversize
        is defense-in-depth against the
        :data:`PAIRING_VERSION_MAX_LEN` cap that the storage
        validator enforces on disk-load.
        """
        data = event.data
        pin_sha256 = data["pin_sha256"]
        self._open_peer_links.add(pin_sha256)
        version = data["esphome_version"]
        if not version or len(version) > PAIRING_VERSION_MAX_LEN:
            return
        pairing = self._pairings.get(pin_sha256)
        if pairing is None or pairing.esphome_version == version:
            return
        pairing.esphome_version = version
        self._schedule_pairings_save()

    def _on_offloader_peer_link_closed(self, event: Event[OffloaderPeerLinkClosedData]) -> None:
        """Discard ``pin_sha256`` from ``_open_peer_links`` on session close."""
        self._open_peer_links.discard(event.data["pin_sha256"])

    def _on_offloader_queue_status_changed(
        self, event: Event[OffloaderQueueStatusChangedData]
    ) -> None:
        """Update the offloader-side ``_peer_queue_status`` cache from a wire event."""
        data = event.data
        self._peer_queue_status[data["pin_sha256"]] = PeerQueueStatusSnapshotEntry(
            receiver_hostname=data["receiver_hostname"],
            receiver_port=data["receiver_port"],
            pin_sha256=data["pin_sha256"],
            idle=data["idle"],
            running=data["running"],
            queue_depth=data["queue_depth"],
        )

    def peer_queue_status_snapshot(self) -> list[PeerQueueStatusSnapshotEntry]:
        """Per-peer queue-status snapshot for ``subscribe_events`` seeding."""
        return list(self._peer_queue_status.values())

    def _on_offloader_job_state_changed(self, event: Event[OffloaderJobStateChangedData]) -> None:
        """Maintain the offloader-side in-flight remote-job cache.

        Upserts the entry on ``queued`` / ``running``; drops on
        terminal (``completed`` / ``failed`` / ``cancelled``)
        so the snapshot only ever carries actively-running
        rows. The :class:`PeerLinkClient` receive loop already
        validated the wire shape before firing this event.
        """
        data = event.data
        if data["status"] in _OFFLOADER_REMOTE_JOB_TERMINAL_STATUSES:
            self._offloader_remote_jobs.pop(data["job_id"], None)
            return
        self._offloader_remote_jobs[data["job_id"]] = OffloaderRemoteJobSnapshotEntry(
            receiver_hostname=data["receiver_hostname"],
            receiver_port=data["receiver_port"],
            pin_sha256=data["pin_sha256"],
            job_id=data["job_id"],
            status=data["status"],
            error_message=data["error_message"],
        )

    def offloader_remote_jobs_snapshot(self) -> list[OffloaderRemoteJobSnapshotEntry]:
        """In-flight remote-job snapshot for ``subscribe_events`` seeding."""
        return list(self._offloader_remote_jobs.values())

    async def _close_peer_link_resolver(self) -> None:
        """Release the shared mDNS-aware aiohttp resolver, if any.

        Idempotent. The borrowed :class:`AsyncZeroconf` is
        closed separately by the device-state monitor.
        """
        if self._peer_link_resolver is None:
            return
        try:
            await self._peer_link_resolver.real_close()
        except Exception:
            _LOGGER.debug("peer-link resolver close failed", exc_info=True)
        self._peer_link_resolver = None

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
        return list(self._peers.values())

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------

    def _offloader_settings_view(self) -> OffloaderRemoteBuildSettingsView:
        """Project the in-RAM offloader-side state to its wire view.

        Pure sync RAM read off :attr:`_pairings` +
        :attr:`_remote_builds_enabled`, which are canonical
        after :meth:`start` seeds them from disk.
        """
        return OffloaderRemoteBuildSettingsView(
            pairings=self.pairings_snapshot(),
            remote_builds_enabled=self._remote_builds_enabled,
        )

    @api_command("remote_build/get_offloader_settings")
    async def get_offloader_settings(self, **kwargs: Any) -> OffloaderRemoteBuildSettingsView:
        """Return the offloader-side settings view (master toggle + pairings list)."""
        return self._offloader_settings_view()

    @api_command("remote_build/set_offloader_settings")
    async def set_offloader_settings(
        self,
        *,
        remote_builds_enabled: bool,
        **kwargs: Any,
    ) -> OffloaderRemoteBuildSettingsView:
        """
        Flip the offloader-side master toggle for transparent install.

        ``False`` short-circuits :func:`pick_build_path` to
        LOCAL; peer-link sessions stay open and the manual
        Send-builds dialog still works. The intent is "keep the
        pairings but stop auto-routing for now."

        Fires ``OFFLOADER_REMOTE_BUILDS_TOGGLED`` for cross-tab
        sync; debounce-saves through ``_pairings_store`` (same
        on-disk shape).
        """
        self._remote_builds_enabled = validate_bool(
            remote_builds_enabled,
            command="remote_build/set_offloader_settings",
            field="remote_builds_enabled",
        )
        toggled: OffloaderRemoteBuildsToggledData = {
            "remote_builds_enabled": remote_builds_enabled,
        }
        self._db.bus.fire(EventType.OFFLOADER_REMOTE_BUILDS_TOGGLED, toggled)
        self._schedule_pairings_save()
        return self._offloader_settings_view()

    @api_command("remote_build/set_pairing_enabled")
    async def set_pairing_enabled(
        self,
        *,
        pin_sha256: str,
        enabled: bool,
        **kwargs: Any,
    ) -> PairingSummary:
        """
        Flip the per-pairing enable switch for transparent install.

        Distinct from ``unpair`` — the row stays in
        ``_pairings``, peer-link client keeps its session open,
        the manual-dispatch surface still works. Disables only
        the auto-routing in ``pick_build_path``.

        Unknown pin raises ``NOT_FOUND``. Fires
        ``OFFLOADER_PAIRING_ENABLED_CHANGED`` for cross-tab
        sync and debounce-saves the pairings store.
        """
        clean_pin = validate_pin_sha256(pin_sha256)
        clean_enabled = validate_bool(
            enabled, command="remote_build/set_pairing_enabled", field="enabled"
        )
        pairing = self._pairings.get(clean_pin)
        if pairing is None:
            msg = f"remote_build/set_pairing_enabled: no pairing for pin_sha256={clean_pin!r}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        pairing.enabled = clean_enabled
        payload: OffloaderPairingEnabledChangedData = {
            "pin_sha256": clean_pin,
            "enabled": clean_enabled,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIRING_ENABLED_CHANGED, payload)
        self._schedule_pairings_save()
        return self._pairing_summary_for(pairing)

    # ------------------------------------------------------------------
    # Offloader-side pair flow — initiator commands that open Noise XX
    # WebSockets to a receiver's peer-link endpoint. The
    # wire-shape driver lives in
    # :mod:`controllers.remote_build_peer_link_client`; the WS command
    # here owns input validation, identity loading, and error mapping.
    # ------------------------------------------------------------------

    @api_command("remote_build/preview_pair")
    async def preview_pair(self, *, hostname: str, port: int, **kwargs: Any) -> dict[str, str]:
        """Open a brief Noise XX WS to *hostname*:*port* and return the receiver's pin.

        ``intent="preview"`` captures the receiver's static
        X25519 pubkey from the handshake transcript. The
        frontend renders the returned ``pin_sha256`` for the
        user to OOB-verify against the receiver's "Build
        server" Settings card before calling ``request_pair``.

        Returns ``{"pin_sha256": "<lowercase-hex-64>"}``.
        """
        clean_host = validate_hostname(hostname, context=HostFieldContext.RECEIVER)
        clean_port = validate_port(port, context=HostFieldContext.RECEIVER)
        loop = asyncio.get_running_loop()
        identity = await loop.run_in_executor(
            None,
            get_or_create_peer_link_identity,
            self._db.settings.config_dir,
        )
        try:
            pin = await peer_link_preview_pair(
                hostname=clean_host,
                port=clean_port,
                identity_priv=identity.private_bytes,
                resolver=self._peer_link_resolver,
            )
        except PeerLinkClientError as exc:
            raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
        return {"pin_sha256": pin}

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
        """Open a Noise XX WS, send ``intent="pair_request"``, persist a local row.

        The second handshake with a receiver, after the user
        OOB-confirmed the receiver's pin via :meth:`preview_pair`.
        Sends ``{"label": offloader_label, "dashboard_id":
        <ours>}`` in the encrypted msg3; the receiver's response
        decides what state the local :class:`StoredPairing` row
        lands in.

        Two labels: *receiver_label* is the offloader-side
        display name (stored locally, never sent); *offloader_label*
        is the offloader's self-identification sent to the
        receiver so its Pairing requests inbox shows a friendly
        name.

        TOCTOU defense: the *pin_sha256* arg is compared against
        the receiver's actual pubkey from the live handshake; a
        mismatch (rotation or MITM) returns
        ``PRECONDITION_FAILED`` and persists nothing.

        Only APPROVED rows reach disk. PENDING lives in-memory
        for the offloader process's lifetime; a restart drops
        them and the user re-runs ``request_pair``.
        """
        clean_host = validate_hostname(hostname, context=HostFieldContext.RECEIVER)
        clean_port = validate_port(port, context=HostFieldContext.RECEIVER)
        clean_pin = validate_pin_sha256(pin_sha256)
        clean_receiver_label = validate_pair_label(
            receiver_label, field=PairLabelField.RECEIVER_LABEL
        )
        clean_offloader_label = validate_pair_label(
            offloader_label, field=PairLabelField.OFFLOADER_LABEL
        )
        peer_link_identity, dashboard_identity = await self._load_offloader_identities_async()

        try:
            result = await peer_link_request_pair(
                hostname=clean_host,
                port=clean_port,
                identity_priv=peer_link_identity.private_bytes,
                label=clean_offloader_label,
                dashboard_id=dashboard_identity.dashboard_id,
                resolver=self._peer_link_resolver,
            )
        except PeerLinkClientError as exc:
            raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc

        enforce_pin_match(expected=clean_pin, observed=result.pin_sha256)
        if (err := intent_response_to_command_error(result.status)) is not None:
            raise err
        if result.status not in (IntentResponse.PENDING, IntentResponse.APPROVED):
            msg = f"unexpected receiver intent_response={result.status.value!r}"
            raise CommandError(ErrorCode.INTERNAL_ERROR, msg)

        # APPROVED here means the receiver short-circuited the
        # inbox dance (re-pair against a still-APPROVED row).
        target_status = (
            PeerStatus.APPROVED if result.status is IntentResponse.APPROVED else PeerStatus.PENDING
        )
        pairing = StoredPairing(
            receiver_hostname=clean_host,
            receiver_port=clean_port,
            pin_sha256=result.pin_sha256,
            static_x25519_pub=result.remote_static_pub,
            label=clean_receiver_label,
            paired_at=time.time(),
            status=target_status,
        )
        key = result.pin_sha256
        # Sweep any stale entry at the same endpoint under a
        # different pin (rotation, or a different receiver took
        # the hostname) so the old row's listener + alert don't
        # orphan under pin-keying.
        self._sweep_stale_pairings_at_endpoint(clean_host, clean_port, keep_pin_sha256=key)
        # Cancel any prior listener for the same pin — its
        # closure captured the old pairing reference.
        self._pairings[key] = pairing
        self._cancel_pair_status_listener(key)
        self._dismiss_offloader_alert(key, clean_host, clean_port)
        if target_status is PeerStatus.APPROVED:
            self._schedule_pairings_save()
            self._spawn_peer_link_client(pairing)
            return self._pairing_summary_for(pairing)
        self._spawn_pair_status_listener(pairing)
        return self._pairing_summary_for(pairing)

    @api_command("remote_build/unpair")
    async def unpair(
        self,
        *,
        pin_sha256: str,
        **kwargs: Any,
    ) -> dict[str, bool]:
        """Drop the local :class:`StoredPairing` row keyed on *pin_sha256*.

        Idempotent — returns ``{"removed": False}`` rather than
        raising on a missing row so the frontend's Unpair button
        always succeeds visually.

        Receiver-side state is **not** notified; the receiver's
        :class:`StoredPeer` row sticks until the receiver's admin
        clicks Remove. The next ``peer_link`` from this offloader
        returns ``REJECTED`` because our local row is gone.

        In-flight pair-status / peer-link tasks for this pin are
        cancelled before mutating the dict so their open Noise WS
        closes promptly.
        """
        key = validate_pin_sha256(pin_sha256)

        # Cancel before mutating the dict so open Noise WSs close
        # promptly. Idempotent on absent keys.
        self._cancel_pair_status_listener(key)
        self._cancel_peer_link_client(key)
        previous = self._pairings.pop(key, None)
        if previous is None:
            return {"removed": False}
        self._schedule_pairings_save()
        self._fire_offloader_pair_status_changed(
            previous.receiver_hostname, previous.receiver_port, key, "removed"
        )
        self._dismiss_offloader_alert(key, previous.receiver_hostname, previous.receiver_port)
        # Drop derived per-peer caches so the snapshot doesn't
        # surface stale data for a row the user just removed.
        self._peer_queue_status.pop(key, None)
        for job_id, entry in list(self._offloader_remote_jobs.items()):
            if entry["pin_sha256"] == key:
                self._offloader_remote_jobs.pop(job_id, None)
        self._open_peer_links.discard(key)
        return {"removed": True}

    @api_command("remote_build/edit_pairing_endpoint")
    async def edit_pairing_endpoint(
        self,
        *,
        pin_sha256: str,
        hostname: str,
        port: int,
        **kwargs: Any,
    ) -> PairingSummary:
        """Manually rebind *pin_sha256*'s pairing onto new (*hostname*, *port*) coords.

        User-driven analog of the mDNS auto-rebind, for cases
        the auto path can't catch: cross-subnet receivers, mDNS
        disabled, receiver moved to a non-broadcast hostname.

        Same trust model: a one-shot ``preview_pair`` probe
        verifies the new endpoint answers with the same pin
        :class:`StoredPairing` was paired against. Pin mismatch
        deliberately doesn't fall through — accepting a new
        identity under the user's existing trust is what the
        re-auth wizard exists for.

        Returns the updated :class:`PairingSummary`;
        ``connected`` typically reads ``False`` because the
        respawned :class:`PeerLinkClient` is still handshaking
        when this method returns.
        """
        pin = validate_pin_sha256(pin_sha256)
        clean_host = validate_hostname(hostname, context=HostFieldContext.RECEIVER)
        clean_port = validate_port(port, context=HostFieldContext.RECEIVER)

        pairing = self._pairings.get(pin)
        if pairing is None:
            msg = f"edit_pairing_endpoint: no pairing for pin_sha256={pin!r}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        if pairing.status is not PeerStatus.APPROVED:
            msg = f"edit_pairing_endpoint: pairing status is {pairing.status.value!r}, not APPROVED"
            raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
        # System-readiness before user-input semantics: surface
        # "identity not loaded yet" distinctly rather than a
        # confusing "matches current" on a startup race.
        if self._offloader_peer_link_priv is None:
            msg = "edit_pairing_endpoint: offloader peer-link identity not loaded yet"
            raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
        if endpoints_equal(
            pairing.receiver_hostname, pairing.receiver_port, clean_host, clean_port
        ):
            msg = f"edit_pairing_endpoint: new endpoint matches current ({clean_host}:{clean_port})"
            raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)

        result = await self._probe_pairing_endpoint(
            pairing=pairing, new_hostname=clean_host, new_port=clean_port
        )
        if result.outcome is not RebindProbeOutcome.OK:
            code, template = EDIT_PAIRING_PROBE_ERRORS[result.outcome]
            raise CommandError(
                code,
                template.format(
                    host=clean_host,
                    port=clean_port,
                    pin=pin,
                    observed=result.observed_pin,
                    error=result.transport_error,
                ),
            )
        self._commit_endpoint_rebind(pairing, hostname=clean_host, port=clean_port)
        return self._pairing_summary_for(pairing)

    async def _validate_submit_job_config(self, configuration: object) -> tuple[str, Path]:
        """Validate the WS *configuration* arg, return ``(name, yaml_path)``.

        Path-traversal boundary via :meth:`DashboardSettings.rel_path`;
        executor hop because ``Path.resolve`` is a syscall. Returns
        the resolved path so the downstream bundle build doesn't
        redo the hop.
        """
        if not isinstance(configuration, str) or not configuration:
            msg = "configuration must be a non-empty string"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        loop = asyncio.get_running_loop()
        yaml_path = await loop.run_in_executor(None, self._db.settings.rel_path, configuration)
        return configuration, yaml_path

    def _lookup_open_peer_link_client(self, pin_sha256: str, *, label: str) -> PeerLinkClient:
        """Return the live :class:`PeerLinkClient` for *pin_sha256*, raising on miss."""
        return peer_link_lifecycle.lookup_open_peer_link_client(self, pin_sha256, label=label)

    async def _build_submit_job_bundle(self, configuration: str, yaml_path: Path) -> bytes:
        """Build the bundle bytes for *yaml_path*.

        Wraps :func:`helpers.config_bundle.build_yaml_bundle`
        (spawns the ``esphome bundle`` CLI). Maps
        :class:`FileNotFoundError` → ``NOT_FOUND`` and
        :class:`BundleBuildError` → ``INVALID_ARGS``; anything
        else propagates to ``INTERNAL_ERROR``. *configuration*
        is the original wire-arg used in diagnostics.
        """
        from ...helpers.config_bundle import (  # noqa: PLC0415
            BundleBuildError,
            build_yaml_bundle,
        )

        try:
            return await build_yaml_bundle(yaml_path)
        except FileNotFoundError as exc:
            raise CommandError(
                ErrorCode.NOT_FOUND, f"submit_job: YAML not found: {configuration}"
            ) from exc
        except BundleBuildError as exc:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"submit_job: bundle build failed for {configuration}: {exc.output or exc}",
            ) from exc

    @api_command("remote_build/submit_job")
    async def submit_job(
        self,
        *,
        pin_sha256: str,
        configuration: str,
        target: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Bundle *configuration* and dispatch a build to the receiver behind *pin_sha256*.

        Offloader-side counterpart of :class:`SubmitJobReceiver`.
        Packs the config + referenced files (includes, secrets,
        fonts, images, …) into a gzipped tarball via the
        ``esphome bundle`` CLI subprocess and streams it over
        the existing peer-link session. Live job lifecycle +
        output ride ``OFFLOADER_JOB_STATE_CHANGED`` /
        ``OFFLOADER_JOB_OUTPUT`` events on the
        ``subscribe_events`` stream; this call returns only the
        receiver's ``submit_job_ack``.

        Subprocess instead of in-process because the CLI is the
        stable upstream contract — in-process ``read_config``
        would couple us to the ESPHome validation pipeline,
        which shifts across releases. Bundle is rebuilt every
        call so a YAML edit can't ship a stale cache.

        Returns ``{"job_id": <our id>, "accepted": <bool>,
        "reason": <str>}`` (``reason`` only on rejection).
        """
        clean_pin = validate_pin_sha256(pin_sha256)
        clean_target = validate_submit_job_target(target)
        clean_config, yaml_path = await self._validate_submit_job_config(configuration)
        client = self._lookup_open_peer_link_client(clean_pin, label="submit_job")
        bundle_bytes = await self._build_submit_job_bundle(clean_config, yaml_path)
        job_id = uuid4().hex[:12]
        try:
            ack = await client.submit_job(
                job_id=job_id,
                configuration_filename=clean_config,
                target=clean_target,
                bundle_bytes=bundle_bytes,
            )
        except PeerLinkNoSessionError as exc:
            raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
        except (SubmitJobTimeoutError, SubmitJobSessionLostError) as exc:
            raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
        result: dict[str, Any] = {
            "job_id": ack["job_id"],
            "accepted": ack["accepted"],
        }
        if "reason" in ack:
            result["reason"] = ack["reason"]
        return result

    @api_command("remote_build/download_artifacts")
    async def download_artifacts(
        self,
        *,
        pin_sha256: str,
        job_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Fetch the build's flash-artifact set for *job_id* from the paired receiver.

        Sends ``download_artifacts{job_id}`` over the live
        peer-link to *pin_sha256*, parks on the assembled-bytes
        future the receive loop fills via
        ``artifacts_start`` / ``_chunk`` / ``_end`` frames,
        unpacks the SHA-256-verified gzipped tarball, and
        rewrites ``idedata.extra.flash_images[].path`` from
        receiver-absolute paths to the bare basenames the
        frontend's install path looks up.

        Returns ``{job_id, idedata, images, total_bytes}`` —
        ``images`` is ``firmware.bin`` first, then
        ``idedata.extra.flash_images`` in declared order.
        """
        clean_pin = validate_pin_sha256(pin_sha256)
        if not isinstance(job_id, str) or not job_id:
            msg = "job_id must be a non-empty string"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        client = self._lookup_open_peer_link_client(clean_pin, label="download_artifacts")
        try:
            packed = await client.download_artifacts(job_id=job_id)
        except PeerLinkNoSessionError as exc:
            raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
        except SubmitJobSessionLostError as exc:
            raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
        except DownloadArtifactsError as exc:
            raise download_artifacts_error_to_command_error(exc) from exc
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, unpack_artifacts_response, packed, job_id
            )
        except UnpackArtifactsError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc

    @api_command("remote_build/cancel_job")
    async def cancel_job(
        self,
        *,
        pin_sha256: str,
        job_id: str,
        **kwargs: Any,
    ) -> dict[str, bool]:
        """Send a ``cancel_job`` frame to the receiver behind *pin_sha256*.

        Fire-and-forget cancel for a previously-submitted
        remote-driven job; the receiver's resulting
        ``job_state_changed{cancelled}`` is the confirmation,
        surfaced via ``OFFLOADER_JOB_STATE_CHANGED``.

        Returns ``{"sent": <bool>}`` reflecting whether the
        frame made it onto the wire; ``sent=false`` is a
        same-tick channel failure the caller should treat as
        an error.
        """
        clean_pin = validate_pin_sha256(pin_sha256)
        if not isinstance(job_id, str) or not job_id:
            msg = "job_id must be a non-empty string"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        client = self._lookup_open_peer_link_client(clean_pin, label="cancel_job")
        try:
            sent = await client.cancel_job(job_id=job_id)
        except PeerLinkNoSessionError as exc:
            raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
        return {"sent": sent}

    def get_pairing(self, pin_sha256: str) -> StoredPairing | None:
        """Return the :class:`StoredPairing` for *pin_sha256*, or ``None``."""
        return self._pairings.get(pin_sha256)

    def remote_builds_enabled_snapshot(self) -> bool:
        """Return the master toggle for the ``subscribe_events`` initial-state seed."""
        return self._remote_builds_enabled

    def build_scheduler_snapshot(self) -> BuildSchedulerInputs:
        """Bundle the scheduler's input state into a shallow immutable snapshot.

        Shallow only — :class:`StoredPairing` rows mutate in
        place elsewhere, but the scheduler runs sync on the
        same loop tick.
        """
        return BuildSchedulerInputs(
            remote_builds_enabled=self._remote_builds_enabled,
            pairings=dict(self._pairings),
            open_peer_links=frozenset(self._open_peer_links),
            peer_queue_status=dict(self._peer_queue_status),
        )

    def pairings_snapshot(self) -> list[PairingSummary]:
        """Return the in-memory pairings (PENDING + APPROVED) for ``subscribe_events`` seeding."""
        return [self._pairing_summary_for(p) for p in self._pairings.values()]

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
        handle = self._peer_link_clients.get(pairing.pin_sha256)
        return pairing_summary(
            pairing,
            connected=pairing.pin_sha256 in self._open_peer_links,
            connecting=handle is not None and handle.client.is_connecting,
            last_connect_error=(handle.client.last_connect_error if handle is not None else ""),
        )

    def offloader_alerts_snapshot(self) -> list[OffloaderAlertSnapshotEntry]:
        """Offloader alerts (insertion-ordered, newest last) for ``subscribe_events`` seeding."""
        return list(self._offloader_alerts.values())

    def _dismiss_offloader_alert(self, pin_sha256: str, hostname: str, port: int) -> bool:
        """Drop the alert for *pin_sha256* and fire DISMISSED. Returns whether a row was dropped.

        Called only by ``request_pair`` (re-pair → stale alert)
        and ``unpair`` (row gone → alert moot). No operator-driven
        dismiss surface — re-pair / unpair are the only resolutions.
        """
        if self._offloader_alerts.pop(pin_sha256, None) is None:
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
            pairings=[p for p in self._pairings.values() if p.status is PeerStatus.APPROVED],
            remote_builds_enabled=self._remote_builds_enabled,
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
        key = pairing.pin_sha256
        existing = self._pair_status_listeners.get(key)
        if existing is not None and not existing.done():
            return
        self._pair_status_listeners[key] = asyncio.create_task(
            self._await_pair_status_flip(pairing),
            name=f"pair-status-{pairing.receiver_hostname}:{pairing.receiver_port}",
        )

    def _cancel_pair_status_listener(self, pin_sha256: str) -> None:
        """Cancel the listener for *pin_sha256*. No-op if none running."""
        task = self._pair_status_listeners.pop(pin_sha256, None)
        if task is not None and not task.done():
            task.cancel()

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
        """Hold a Noise WS to the receiver until the row flips status.

        Single-shot: opens one Noise WS with ``intent="pair_status"``,
        awaits the receiver's response (which the receiver-side
        responder holds open until its own bus fires
        ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` for the matching
        ``dashboard_id``), persists the result + fires
        ``OFFLOADER_PAIR_STATUS_CHANGED``, then exits. On transport
        error, sleeps :data:`_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS`
        and reconnects.
        """
        peer_link_identity, dashboard_identity = await self._load_offloader_identities_async()
        try:
            while True:
                try:
                    result = await peer_link_await_pair_status(
                        hostname=pairing.receiver_hostname,
                        port=pairing.receiver_port,
                        identity_priv=peer_link_identity.private_bytes,
                        dashboard_id=dashboard_identity.dashboard_id,
                        resolver=self._peer_link_resolver,
                    )
                except PeerLinkClientError as exc:
                    _LOGGER.debug(
                        "pair-status listener for %s:%s reconnecting: %s",
                        pairing.receiver_hostname,
                        pairing.receiver_port,
                        exc,
                    )
                    await asyncio.sleep(_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS)
                    continue
                terminal = await self._apply_pair_status_result(pairing, result)
                if terminal:
                    return
                # Non-terminal result reached the apply path —
                # only happens on a misbehaving receiver returning
                # an unexpected ``intent_response`` (PENDING / OK /
                # NO_PAIRING_WINDOW from a `pair_status` query
                # shouldn't happen). Back off before reconnecting
                # so a bug in the receiver doesn't burn CPU /
                # spam logs in a tight reconnect loop.
                await asyncio.sleep(_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS)
        finally:
            # Only clear the slot if it still points at this task.
            # On a re-pair, ``_cancel_pair_status_listener`` has
            # already popped this task and ``_spawn_pair_status_listener``
            # has put the replacement in the slot — blindly
            # ``pop()``-ing here would evict the replacement and
            # orphan it (no entry left for ``unpair`` to cancel,
            # the new listener parks forever).
            key = pairing.pin_sha256
            if self._pair_status_listeners.get(key) is asyncio.current_task():
                del self._pair_status_listeners[key]

    async def _apply_pair_status_result(
        self, pairing: StoredPairing, result: PairStatusResult
    ) -> bool:
        """Apply a pair-status response. Return True when the listener should exit.

        * APPROVED + matching pin → flip the row to APPROVED.
        * APPROVED + drifted pin → drop the row (peer-revoked;
          new pubkey under existing trust requires re-pair).
        * REJECTED → drop the row (admin rejected, window
          closed, offloader rotated, or row never existed).
        * Anything else → log + reconnect.

        Race-safe against ``unpair``: every branch keys on
        ``self._pairings.pop(key, None)``, so if the user
        unpaired between the await and this branch we skip
        promotion + event-firing silently.
        """
        host = pairing.receiver_hostname
        port = pairing.receiver_port
        # Captured before the dict mutates — alerts fire
        # alongside ``status="removed"`` and need the label.
        label = pairing.label
        stored_pin = pairing.pin_sha256
        key = pairing.pin_sha256
        if result.status is IntentResponse.APPROVED:
            if result.pin_sha256 != pairing.pin_sha256:
                _LOGGER.warning(
                    "pair-status pin drift for %s:%s; dropping row (stored=%s observed=%s)",
                    host,
                    port,
                    pairing.pin_sha256,
                    result.pin_sha256,
                )
                if self._pairings.pop(key, None) is not None:
                    self._schedule_pairings_save()
                    pin_alert: OffloaderPinMismatchAlert = {
                        "kind": "pin_mismatch",
                        "receiver_hostname": host,
                        "receiver_port": port,
                        "pin_sha256": stored_pin,
                        "receiver_label": label,
                        "expected_pin": stored_pin,
                        "observed_pin": result.pin_sha256,
                        "fired_at": time.time(),
                    }
                    self._offloader_alerts[key] = pin_alert
                    # Fire diagnostic first so subscribers see
                    # the full payload before the row drops.
                    self._fire_offloader_pair_pin_mismatch(
                        host, port, key, label, stored_pin, result.pin_sha256
                    )
                    self._fire_offloader_pair_status_changed(host, port, key, "removed")
                return True
            # PENDING → APPROVED in place. If ``unpair`` raced
            # us between the await and this branch the row's
            # gone; exit silently rather than resurrect state
            # the user just deleted.
            existing = self._pairings.get(key)
            if existing is None:
                return True
            existing.status = PeerStatus.APPROVED
            self._schedule_pairings_save()
            self._fire_offloader_pair_status_changed(host, port, key, "approved")
            self._spawn_peer_link_client(existing)
            return True
        if result.status is IntentResponse.REJECTED:
            if self._pairings.pop(key, None) is not None:
                self._schedule_pairings_save()
                revoked_alert: OffloaderPeerRevokedAlert = {
                    "kind": "peer_revoked",
                    "receiver_hostname": host,
                    "receiver_port": port,
                    "pin_sha256": stored_pin,
                    "receiver_label": label,
                    "fired_at": time.time(),
                }
                self._offloader_alerts[key] = revoked_alert
                self._fire_offloader_pair_peer_revoked(host, port, key, label)
                self._fire_offloader_pair_status_changed(host, port, key, "removed")
            return True
        _LOGGER.warning(
            "pair-status returned unexpected status %r for %s:%s",
            result.status,
            host,
            port,
        )
        return False

    def _fire_offloader_pair_status_changed(
        self,
        receiver_hostname: str,
        receiver_port: int,
        pin_sha256: str,
        status: Literal["approved", "removed"],
    ) -> None:
        """Fire ``OFFLOADER_PAIR_STATUS_CHANGED`` for a pairing flip."""
        payload: OffloaderPairStatusChangedData = {
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
            "pin_sha256": pin_sha256,
            "status": status,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_STATUS_CHANGED, payload)

    def _fire_offloader_pair_pin_mismatch(
        self,
        receiver_hostname: str,
        receiver_port: int,
        pin_sha256: str,
        receiver_label: str,
        expected_pin: str,
        observed_pin: str,
    ) -> None:
        """Fire ``OFFLOADER_PAIR_PIN_MISMATCH`` for a drifted-pin pair_status."""
        payload: OffloaderPairPinMismatchData = {
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
            "receiver_label": receiver_label,
            "pin_sha256": pin_sha256,
            "expected_pin": expected_pin,
            "observed_pin": observed_pin,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_PIN_MISMATCH, payload)

    def _fire_offloader_pair_peer_revoked(
        self,
        receiver_hostname: str,
        receiver_port: int,
        pin_sha256: str,
        receiver_label: str,
    ) -> None:
        """Fire ``OFFLOADER_PAIR_PEER_REVOKED`` for a REJECTED pair_status."""
        payload: OffloaderPairPeerRevokedData = {
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
            "receiver_label": receiver_label,
            "pin_sha256": pin_sha256,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_PEER_REVOKED, payload)

    # ------------------------------------------------------------------
    # Identity — surface the receiver's own dashboard_id + cert pin to
    # the Settings UI without making it reach into the cert PEM
    # directly. Rotation lives next door so the "rotate" button can
    # land in the same controller.
    # ------------------------------------------------------------------
