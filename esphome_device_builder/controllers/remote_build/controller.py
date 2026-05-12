"""
Remote-build feature: peer dashboard discovery + pairing + peers.

Browses ``_esphomebuilder._tcp.local.`` for other dashboards,
persists the receiver-side ``enabled`` flag, the paired-peer
list, and the offloader-side pairings, and surfaces a unified
``remote_build/list_hosts`` snapshot.

Pairing is a two-step gate: an offloader's ``pair_request``
lands a PENDING row inside the receiver-controlled pairing
window; the admin clicks Accept (``approve_peer``) and APPROVED
peers connect anytime via ``intent="peer_link"``. Receiver-side
:class:`StoredPeer` rows are keyed on ``dashboard_id`` and
carry the X25519 ``pin_sha256`` + ``static_x25519_pub`` from
the handshake transcript.

Browser shares the :class:`AsyncEsphomeZeroconf` the
:class:`DeviceStateMonitor` owns (one mDNS responder per
process); this controller just adds a second
:class:`~zeroconf.asyncio.AsyncServiceBrowser` for the new
service type.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Hashable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ...helpers import dashboard_identity as _dashboard_identity_helper
from ...helpers.api import CommandError, api_command
from ...helpers.build_scheduler import BuildSchedulerInputs
from ...helpers.dashboard_advertise import SERVICE_TYPE
from ...helpers.dashboard_identity import get_or_create_identity
from ...helpers.event_bus import Event
from ...helpers.hostname import normalize_hostname
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...helpers.peer_link_identity import get_or_create_peer_link_identity
from ...helpers.peer_link_resolver import PeerLinkDNSResolver, make_peer_link_resolver
from ...helpers.remote_build_cleanup import sweep_remote_builds
from ...helpers.remote_build_layout import parse_from_configuration
from ...helpers.storage import ShutdownCallback, Store
from ...models import (
    MAX_CLEANUP_TTL_SECONDS,
    MIN_CLEANUP_TTL_SECONDS,
    PAIRING_VERSION_MAX_LEN,
    TERMINAL_JOB_EVENTS,
    ErrorCode,
    EventType,
    IdentityView,
    IntentResponse,
    OffloaderAlertSnapshotEntry,
    OffloaderJobStateChangedData,
    OffloaderPairAlertDismissedData,
    OffloaderPairEndpointReboundData,
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
    PairingWindowState,
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    PeerSummary,
    QueueStatusFrameData,
    ReceiverPeerLinkSessionClosedData,
    ReceiverPeerLinkSessionOpenedData,
    ReceiverPeers,
    RemoteBuildHostRemovedData,
    RemoteBuildIdentityRotatedData,
    RemoteBuildPairingWindowChangedData,
    RemoteBuildPairRequestReceivedData,
    RemoteBuildPairStatusChangedData,
    RemoteBuildPeer,
    RemoteBuildSettings,
    RemoteBuildSettingsView,
    StoredPairing,
    StoredPeer,
)
from ..config import (
    load_remote_build_settings,
    remote_build_settings_transaction,
)
from ._mdns import endpoints_equal, peer_from_service_info
from ._models import (
    EDIT_PAIRING_PROBE_ERRORS,
    PeerLinkClientHandle,
    RebindProbeOutcome,
    RebindProbeResult,
)
from ._storage_codecs import (
    OFFLOADER_PAIRINGS_FILE,
    RECEIVER_PEERS_FILE,
    decode_pairings,
    decode_peers,
    encode_pairings,
    encode_peers,
)
from ._summaries import identity_view, pairing_summary, peer_summary
from ._validators import (
    HostFieldContext,
    PairLabelField,
    download_artifacts_error_to_command_error,
    enforce_pin_match,
    intent_response_to_command_error,
    validate_bool,
    validate_dashboard_id,
    validate_hostname,
    validate_pair_label,
    validate_pin_sha256,
    validate_port,
    validate_submit_job_target,
)
from .artifacts_download import ArtifactsDownloadSender
from .artifacts_tarball import UnpackArtifactsError, unpack_artifacts_response
from .job_fanout import JobFanout
from .peer_link import PeerLinkSession, TerminateReason
from .peer_link_client import (
    DownloadArtifactsError,
    PairStatusResult,
    PeerLinkClient,
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
from .submit_job import SubmitJobReceiver

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.dashboard_identity import DashboardIdentity
    from ...helpers.peer_link_identity import PeerLinkIdentity

_LOGGER = logging.getLogger(__name__)


def _load_offloader_identities(
    config_dir: Path,
) -> tuple[PeerLinkIdentity, DashboardIdentity]:
    """Load both offloader-side identities in one executor hop.

    Bundling keeps the async caller's body to a single
    ``await`` instead of two.
    """
    return get_or_create_peer_link_identity(config_dir), get_or_create_identity(config_dir)


# Cache-miss resolve timeout for the dashboard service-info
# fetch. Longer than the device-state monitor's because peer
# dashboards run on full hosts that may be more LAN hops away.
_RESOLVE_TIMEOUT_MS = 3000

# Pairing-window lifetime. Auto-closes after this much idle;
# the frontend extends on each activity tick.
_PAIRING_WINDOW_DURATION_SECONDS = 300.0


# Terminal status set for the offloader-side remote-job cache
# drop-on-terminal logic.
_OFFLOADER_REMOTE_JOB_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)


# Required fields on inbound ``cancel_job`` peer-link frames.
_CANCEL_JOB_SCHEMA = frame_schema({"job_id": str})


# Reconnect backoff for a pair-status listener whose Noise WS
# died on transport error — bounds tight-looping against a
# hard-down receiver.
_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS = 2.0

# Debounce window for the offloader-side pairings-store write
# so a burst of approvals collapses to one disk write.
_PAIRINGS_SAVE_DELAY_SECONDS = 1.0

# Cleanup-sweep cadence — TTL itself is the
# operator-tunable knob (:data:`DEFAULT_CLEANUP_TTL_SECONDS`).
_CLEANUP_SWEEP_INTERVAL_SECONDS = 60 * 60

# Per-pin sliding window between mDNS rebind probes. Doubles
# as in-flight guard + retry throttle so a permanently-down
# host doesn't trigger a probe per mDNS Updated burst.
_REBIND_PROBE_COOLDOWN_SECONDS = 30.0


class RemoteBuildController:  # noqa: PLR0904 (grandfathered; new public methods need a refactor first)
    """
    Discover peer dashboards and own the receiver-side settings.

    Constructed once in :meth:`DeviceBuilder.start`. The browser
    lifetime is tied to :meth:`start` / :meth:`stop`; the controller's
    own start happens after :class:`DevicesController.start` so the
    shared zeroconf instance is already up.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._browser: AsyncServiceBrowser | None = None
        # Shared ``aiohttp`` resolver wired to the dashboard's
        # :class:`AsyncEsphomeZeroconf` so outbound peer-link
        # connects resolve ``*.local`` hostnames via mDNS rather
        # than ``getaddrinfo``. ``None`` when zeroconf isn't up;
        # call sites fall back to aiohttp's default resolver.
        self._peer_link_resolver: PeerLinkDNSResolver | None = None
        self._peers: dict[str, RemoteBuildPeer] = {}
        # Strong refs for fire-and-forget resolve tasks (GC can't
        # reap them mid-await).
        self._tasks: set[asyncio.Task[None]] = set()
        # mDNS auto-rebind probe slot per pin → monotonic
        # deadline. Doubles as in-flight guard + retry throttle:
        # a probe storm from mDNS Updated bursts collapses to one
        # probe per cooldown. Successful probes clear the entry.
        self._rebind_probe_until: dict[str, float] = {}
        # Own service-instance name (captured at start) so we
        # filter our own broadcast out of the discovered list.
        self._own_instance_name: str | None = None
        # True while ``rotate_identity`` is in flight. Second
        # caller gets ``ALREADY_EXISTS`` rather than queuing —
        # interleaved teardowns can leave no listener at all,
        # and back-to-back rotation is almost always an
        # accidental double-click.
        self._rotation_in_flight = False
        # Pairing window: gates ``pair_request``, refcounted by
        # WS client so multi-tab admins extend together.
        # APPROVED peers bypass the window via ``peer_link``.
        self._pairing_window_clients: dict[Hashable, float] = {}
        self._pairing_window_handle: asyncio.TimerHandle | None = None
        # One Task per PENDING StoredPairing holding an open
        # pair_status long-poll. Spawned by ``request_pair``,
        # cancelled by ``unpair`` / re-pair / terminal-flip. Keyed
        # on ``pin_sha256``; RAM-only (PENDING never persists).
        self._pair_status_listeners: dict[str, asyncio.Task[None]] = {}
        # PENDING StoredPeer rows keyed on ``dashboard_id``;
        # never persisted, cleared on window auto-close (bounds
        # LAN-scanner spam). Clears fire ``status="removed"`` so
        # in-flight long-polls wake.
        self._pending_peers: dict[str, StoredPeer] = {}
        # RAM-canonical APPROVED peers keyed on ``dashboard_id``;
        # disk is just persistence.
        self._approved_peers: dict[str, StoredPeer] = {}
        # Live peer-link sessions keyed on offloader's
        # ``dashboard_id``. One per dashboard_id; a duplicate
        # connect kicks the older session via
        # ``TerminateReason.SUPERSEDED`` so a restarted offloader
        # takes its slot back rather than doubling.
        self._peer_link_sessions: dict[str, PeerLinkSession] = {}
        # Receiver-side handlers; constructed in :meth:`start`
        # once the firmware controller is available. Their
        # accessor methods raise if reached before bind.
        self._submit_job_receiver: SubmitJobReceiver | None = None
        self._artifacts_download_sender: ArtifactsDownloadSender | None = None
        # Fan-out from firmware ``JOB_*`` events to peer-link
        # frames, filtered to remote-peer jobs.
        self._job_fanout: JobFanout | None = None
        # One peer-link client per APPROVED pairing, keyed on
        # ``pin_sha256``. Handle bundles the task with the
        # client; WS commands reach the submit/cancel API
        # through the client.
        self._peer_link_clients: dict[str, PeerLinkClientHandle] = {}
        # Currently-open offloader-side peer-link sessions
        # (toggled by OPENED/CLOSED listeners). Read by
        # ``pairings_snapshot`` to fill ``connected``.
        self._open_peer_links: set[str] = set()
        # Cached at :meth:`start`. WS commands re-read from
        # disk via :meth:`_load_offloader_identities_async` to
        # pick up rotations.
        self._offloader_dashboard_id: str | None = None
        self._offloader_peer_link_priv: bytes | None = None
        # Offloader pairings (PENDING + APPROVED) keyed on
        # ``pin_sha256`` (cryptographic identity); routing
        # hints live as fields so receiver-rename is a value
        # mutation. Disk filter strips PENDING at serialise.
        self._pairings: dict[str, StoredPairing] = {}
        # Master "remote builds enabled" toggle for the
        # offloader-side install scheduler.
        self._remote_builds_enabled: bool = True
        # RAM-only pair alerts (pin_mismatch / peer_revoked);
        # cleared only by re-pair or unpair.
        self._offloader_alerts: dict[str, OffloaderAlertSnapshotEntry] = {}
        # Most recent queue_status per paired receiver.
        self._peer_queue_status: dict[str, PeerQueueStatusSnapshotEntry] = {}
        # In-flight remote jobs keyed on offloader-local
        # ``job_id``; rows drop on terminal status.
        self._offloader_remote_jobs: dict[str, OffloaderRemoteJobSnapshotEntry] = {}
        # ``Store`` instances register their flush callbacks
        # here; :meth:`stop` walks them to drain debounced writes.
        self._shutdown_callbacks: list[ShutdownCallback] = []
        self._pairings_store: Store[OffloaderRemoteBuildSettings] = Store(
            self._db.settings.config_dir / OFFLOADER_PAIRINGS_FILE,
            encoder=encode_pairings,
            decoder=decode_pairings,
            shutdown_register=self._shutdown_callbacks.append,
            name="offloader_pairings",
        )
        self._peers_store: Store[ReceiverPeers] = Store(
            self._db.settings.config_dir / RECEIVER_PEERS_FILE,
            encoder=encode_peers,
            decoder=decode_peers,
            shutdown_register=self._shutdown_callbacks.append,
            name="receiver_peers",
        )
        # Bus-listener unsubscribers; :meth:`stop` closes the
        # stack to detach all of them in one pass.
        self._listeners = ExitStack()

    async def start(self) -> None:
        """Bring up the receiver-side handlers, seed RAM from disk, spawn clients."""
        # Receiver-side handlers depend on the firmware controller.
        if self._db.firmware is not None:
            self._submit_job_receiver = SubmitJobReceiver(
                config_dir=self._db.settings.config_dir,
                firmware_controller=self._db.firmware,
            )
            self._artifacts_download_sender = ArtifactsDownloadSender(
                firmware_controller=self._db.firmware,
            )
            self._job_fanout = JobFanout(self)
            self._job_fanout.start()
            self._track_task(
                self._run_cleanup_loop(),
                name=f"{type(self).__name__}._run_cleanup_loop",
            )
        if (settings := await self._pairings_store.async_load()) is not None:
            for pairing in settings.pairings:
                self._pairings[pairing.pin_sha256] = pairing
            self._remote_builds_enabled = settings.remote_builds_enabled
        if (peers_state := await self._peers_store.async_load()) is not None:
            for peer in peers_state.peers:
                self._approved_peers[peer.dashboard_id] = peer
        peer_link_identity, dashboard_identity = await self._load_offloader_identities_async()
        self._offloader_peer_link_priv = peer_link_identity.private_bytes
        self._offloader_dashboard_id = dashboard_identity.dashboard_id
        # Wire the resolver before spawning clients so each picks
        # it up at construction; stays None if zeroconf is down
        # (HA-addon without ``ports:``, monitor failed to bind)
        # and outbound connects fall back to the OS resolver.
        self._setup_peer_link_resolver()
        for pairing in self._pairings.values():
            if pairing.status is PeerStatus.APPROVED:
                self._spawn_peer_link_client(pairing)
        # JOB_OUTPUT / JOB_PROGRESS deliberately omitted from the
        # broadcast set: high-rate streaming events that don't
        # change queue_status shape.
        for event_type in (
            EventType.JOB_QUEUED,
            EventType.JOB_STARTED,
            *TERMINAL_JOB_EVENTS,
        ):
            self._listeners.callback(
                self._db.bus.add_listener(event_type, self._on_firmware_queue_transition)
            )
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

    async def _load_settings_async(self) -> RemoteBuildSettings:
        """Read the receiver-side settings sidecar off the executor.

        Carries the ``enabled`` master toggle +
        ``cleanup_ttl_seconds`` knobs, which aren't mirrored in
        RAM (the RAM-canonical state is :attr:`_approved_peers`
        / :attr:`_pairings`).
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
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
        """
        Bring up the mDNS service browser for peer discovery.

        Captures the dashboard's own service-instance name (so
        our own advertise doesn't show up in ``list_hosts``) and
        constructs the :class:`AsyncServiceBrowser` against the
        shared zeroconf. Skips silently if either the devices
        controller or its zeroconf isn't available (peer
        discovery is opt-in fail-soft); on browser-construction
        failure logs the exception and leaves :attr:`_browser`
        as ``None``.
        """
        if self._db.devices is None:
            _LOGGER.debug("remote-build discovery skipped: devices controller unavailable")
            return
        zeroconf = self._db.devices.zeroconf
        if zeroconf is None:
            _LOGGER.debug("remote-build discovery skipped: zeroconf unavailable")
            return
        # Capture own service-instance name so our own advertise
        # doesn't show up in ``list_hosts``. Reads through the
        # public ``service_instance_name`` accessor on
        # ``DashboardAdvertiser`` rather than reaching into
        # ``_info``; keeps this controller decoupled from the
        # advertiser's private layout.
        advertiser = self._db._dashboard_advertiser
        if advertiser is not None:
            self._own_instance_name = advertiser.service_instance_name
        # Wrap browser construction so a zeroconf-side failure
        # (e.g. the underlying socket got torn down between
        # ``DeviceStateMonitor.start`` and now, or the cache is in
        # an unexpected state) doesn't abort dashboard startup.
        try:
            self._browser = AsyncServiceBrowser(
                zeroconf.zeroconf,
                [SERVICE_TYPE],
                handlers=[self._on_service_state_change],
            )
        except Exception:
            _LOGGER.exception("Could not start remote-build browser; peer discovery disabled")
            self._browser = None

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

    def _on_firmware_queue_transition(self, event: Event[Any]) -> None:
        """Bus listener: broadcast ``queue_status`` to paired offloaders.

        Called on every ``JOB_QUEUED`` / ``JOB_STARTED`` /
        terminal event. Builds a snapshot from the firmware
        controller's RAM state (sync read, no awaitables in the
        bus listener) and schedules a per-session broadcast as a
        background task. The broadcast itself runs async because
        it sends across N peer-link sessions and we don't want a
        slow socket on one session to block other listeners
        observing the same event.
        """
        if self._db.firmware is None:
            return
        idle, running, queue_depth = self._db.firmware.queue_status_snapshot()
        if not self._peer_link_sessions:
            return
        self._db.create_background_task(self._broadcast_queue_status(idle, running, queue_depth))

    async def _broadcast_queue_status(self, idle: bool, running: bool, queue_depth: int) -> None:
        """Send a ``queue_status`` frame to every active peer-link session.

        Snapshot the registry to a list before iterating so a
        concurrent register / unregister mid-walk doesn't mutate
        the dict under us. Each ``send_app_frame`` is gated on
        the session's ``_closing`` flag, so a ``terminate``-in-
        progress session no-ops cleanly here without raising.
        Per-session failures (``send_app_frame`` returns
        ``False``) are logged at the channel layer; we don't
        retry — a session that can't accept the latest snapshot
        will pick up the next transition's broadcast on its
        next successful frame.
        """
        sessions = list(self._peer_link_sessions.values())
        payload = QueueStatusFrameData(
            type="queue_status",
            idle=idle,
            running=running,
            queue_depth=queue_depth,
        )
        for session in sessions:
            # Per-session try/except so one flaky peer can't
            # starve broadcasts to its siblings; the next queue
            # transition will retry.
            try:
                await session.send_app_frame(dict(payload))
            except Exception:
                _LOGGER.exception(
                    "queue_status broadcast to session %s raised; continuing with siblings",
                    session.dashboard_id,
                )

    async def register_peer_link_session(self, session: PeerLinkSession) -> None:
        """Register *session*; evict a stale same-``dashboard_id`` slot via SUPERSEDED.

        Install runs before the terminate await so concurrent
        dispatches see the freshest entry. Pushes an initial
        ``queue_status`` to the offloader — without it,
        cold-connected pairings never get an entry in
        ``_peer_queue_status`` and ``pick_build_path`` silently
        falls back to LOCAL (#568 regression).
        """
        existing = self._peer_link_sessions.get(session.dashboard_id)
        self._peer_link_sessions[session.dashboard_id] = session
        if existing is not None and existing is not session:
            await existing.terminate(TerminateReason.SUPERSEDED)
        if self._db.firmware is not None:
            try:
                idle, running, queue_depth = self._db.firmware.queue_status_snapshot()
            except Exception:
                # Best-effort: the transition-driven broadcast
                # catches up the offloader on the next change.
                _LOGGER.exception(
                    "firmware.queue_status_snapshot() raised on session register; "
                    "skipping initial queue_status push to %s",
                    session.dashboard_id,
                )
            else:
                self._db.create_background_task(
                    self._send_initial_queue_status(session, idle, running, queue_depth)
                )
        # Fire AFTER the dict insert so subscriber lookups see
        # the just-registered session.
        if self._db.bus is not None:
            self._db.bus.fire(
                EventType.RECEIVER_PEER_LINK_SESSION_OPENED,
                ReceiverPeerLinkSessionOpenedData(dashboard_id=session.dashboard_id),
            )

    async def _send_initial_queue_status(
        self,
        session: PeerLinkSession,
        idle: bool,
        running: bool,
        queue_depth: int,
    ) -> None:
        """Push a one-shot ``queue_status`` frame to a freshly-connected session.

        Mirror of :meth:`_broadcast_queue_status` but addressed
        to a single session — invoked from
        :meth:`register_peer_link_session` so the offloader gets
        an idle / running signal on cold-connect rather than
        waiting for the receiver's next firmware queue
        transition. Best-effort: a session that has already
        torn down between the registry insert and this send
        no-ops cleanly (``send_app_frame`` is gated on the
        session's ``_closing`` flag) and the offloader's next
        reconnect tries again.
        """
        payload = QueueStatusFrameData(
            type="queue_status",
            idle=idle,
            running=running,
            queue_depth=queue_depth,
        )
        try:
            await session.send_app_frame(dict(payload))
        except Exception:
            _LOGGER.exception(
                "initial queue_status to session %s raised; "
                "offloader will catch up on the next queue transition",
                session.dashboard_id,
            )

    def unregister_peer_link_session(self, session: PeerLinkSession) -> None:
        """
        Drop *session* from the active peer-link registry.

        No-op when a different session has taken the slot (the
        :meth:`register_peer_link_session` dedupe path replaces
        the entry before the old session's loop unwinds; the old
        loop's ``finally`` calls this and would otherwise evict
        the new entry). Sync because it's just a dict pop — the
        actual WS close + Noise teardown happens in the session
        loop's ``finally`` chain.
        """
        if self._peer_link_sessions.get(session.dashboard_id) is session:
            del self._peer_link_sessions[session.dashboard_id]
            # Drop any in-flight ``submit_job`` upload state so a
            # bundle reception that was mid-stream when the
            # session ended doesn't outlive the session that owns
            # it. ``_submit_job_receiver`` is set in :meth:`start`;
            # this branch only runs for sessions registered after
            # ``start`` (live wire), so the attribute is always
            # populated by the time we get here.
            if self._submit_job_receiver is not None:
                self._submit_job_receiver.discard_session(session.dashboard_id)
            # Same shape — discard any in-flight artifacts
            # download for this session so the slot doesn't
            # outlive the session it was streaming over.
            if self._artifacts_download_sender is not None:
                self._artifacts_download_sender.discard_session(session.dashboard_id)
            # Fire only when we actually dropped the slot — the
            # no-op path (a SUPERSEDED-evicted session running its
            # finally-block after the new session has taken its
            # place) would double-fire CLOSED for a single
            # logical close otherwise.
            if self._db.bus is not None:
                self._db.bus.fire(
                    EventType.RECEIVER_PEER_LINK_SESSION_CLOSED,
                    ReceiverPeerLinkSessionClosedData(dashboard_id=session.dashboard_id),
                )

    async def handle_cancel_job(self, session: PeerLinkSession, frame: dict[str, Any]) -> None:
        """Receiver-side dispatch for inbound ``cancel_job`` frames.

        Resolves the offloader's ``job_id`` to the receiver-local
        :class:`FirmwareJob` via :class:`JobFanout` and routes
        through :meth:`FirmwareController.cancel` — same path as
        an operator-driven cancel. No wire ack; the fan-out's
        ``job_state_changed{cancelled}`` carries the result.

        Silent debug-log drops for malformed frames, unknown
        correlations (race with a terminal transition), and
        :class:`CommandError` from cancel (already-terminal).
        """
        if not is_valid_frame(_CANCEL_JOB_SCHEMA, frame):
            _LOGGER.debug(
                "peer-link cancel_job from %s: malformed frame; dropping: %r",
                session.dashboard_id,
                frame,
            )
            return
        if self._job_fanout is None or self._db.firmware is None:
            _LOGGER.debug(
                "peer-link cancel_job from %s before controller fully started; dropping",
                session.dashboard_id,
            )
            return
        remote_job_id = cast(str, frame["job_id"])
        firmware_job_id = self._job_fanout.resolve_firmware_job_id(
            session.dashboard_id, remote_job_id
        )
        if firmware_job_id is None:
            _LOGGER.debug(
                "peer-link cancel_job from %s: no firmware job for remote_job_id=%r; dropping",
                session.dashboard_id,
                remote_job_id,
            )
            return
        try:
            await self._db.firmware.cancel(job_id=firmware_job_id)
        except CommandError as exc:
            _LOGGER.debug(
                "peer-link cancel_job from %s: firmware refused cancel for job %s: %s",
                session.dashboard_id,
                firmware_job_id,
                exc.message,
            )

    def get_submit_job_receiver(self) -> SubmitJobReceiver:
        """Return the receiver-side ``submit_job`` flow handler, raising if not started.

        Method (not ``@property``) because
        :func:`collect_api_commands` walks public attributes
        at startup; a property getter would fire pre-``start``
        and raise.
        """
        if self._submit_job_receiver is None:
            msg = "submit_job_receiver accessed before RemoteBuildController.start()"
            raise RuntimeError(msg)
        return self._submit_job_receiver

    def get_artifacts_download_sender(self) -> ArtifactsDownloadSender:
        """Return the receiver-side ``download_artifacts`` flow handler, raising if not started."""
        if self._artifacts_download_sender is None:
            msg = "artifacts_download_sender accessed before RemoteBuildController.start()"
            raise RuntimeError(msg)
        return self._artifacts_download_sender

    async def stop(self) -> None:
        """Cancel the browser, drain tasks + sessions, flush stores."""
        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception:
                _LOGGER.debug("remote-build browser cancel failed", exc_info=True)
            self._browser = None
        self._listeners.close()
        if self._job_fanout is not None:
            self._job_fanout.stop()
            self._job_fanout = None
        await self._drain_tasks(self._tasks)
        self._tasks.clear()
        await self._drain_tasks(self._pair_status_listeners.values())
        self._pair_status_listeners.clear()
        # Each peer-link client's CancelledError handler sends a
        # ``client_stopped`` terminate so the receiver doesn't wait
        # on its heartbeat to time out.
        await self._drain_tasks(h.task for h in self._peer_link_clients.values())
        self._peer_link_clients.clear()
        if self._pairing_window_handle is not None:
            self._pairing_window_handle.cancel()
            self._pairing_window_handle = None
        self._pairing_window_clients.clear()
        # Snapshot to a list before iterating — each terminate
        # unwinds via ``unregister_peer_link_session`` which
        # mutates the dict.
        for peer_link_session in list(self._peer_link_sessions.values()):
            await peer_link_session.terminate(TerminateReason.SERVER_SHUTTING_DOWN)
        self._peer_link_sessions.clear()
        # Fire ``status="removed"`` for each PENDING peer so
        # in-flight pair_status long-polls on a still-alive bus
        # see the cancellation (matters for the soft-reload path).
        self._clear_pending_peers_on_window_close()
        # Flush debounced writes from every registered Store
        # before the dicts go away.
        for callback in self._shutdown_callbacks:
            await callback()
        self._pairings.clear()
        self._peer_queue_status.clear()
        self._offloader_remote_jobs.clear()
        self._open_peer_links.clear()
        self._rebind_probe_until.clear()
        self._peers.clear()
        self._approved_peers.clear()
        await self._close_peer_link_resolver()

    @staticmethod
    async def _drain_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
        """Cancel and await every task in *tasks*, swallowing exceptions.

        Snapshots *tasks* to a list so the caller's post-drain
        ``clear`` doesn't pull tasks out from under the gather.
        Caller owns clearing the source collection.
        """
        tasks_list = list(tasks)
        if not tasks_list:
            return
        for task in tasks_list:
            task.cancel()
        await asyncio.gather(*tasks_list, return_exceptions=True)

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
        """
        Browser callback; resolve the service info and update the peer map.

        Filters our own service-instance name so we don't surface
        our own advertise as a discovered host. ``Removed`` events
        delete the peer immediately and fire
        :attr:`EventType.REMOTE_BUILD_HOST_REMOVED`; ``Added`` /
        ``Updated`` resolve either from the zeroconf cache (sync,
        fires :attr:`EventType.REMOTE_BUILD_HOST_ADDED` inline) or
        via a fire-and-forget task (async, fires from
        :meth:`_resolve_and_apply` once the SRV / TXT round-trip
        completes).
        """
        if name == self._own_instance_name:
            return
        if state_change == ServiceStateChange.Removed:
            popped = self._peers.pop(name, None)
            if popped is not None:
                # Event keys on the wire-friendly ``peer.name``
                # (leftmost label) so frontend dicts keyed on the
                # ``RemoteBuildPeer.name`` field upsert/delete
                # consistently. The FQDN ``name`` is the
                # ``self._peers`` dict key only.
                self._fire_host_removed(popped.name)
            return
        info = AsyncServiceInfo(service_type, name)
        if info.load_from_cache(zeroconf):
            self._upsert_host(name, info)
            return
        self._track_task(self._resolve_and_apply(zeroconf, info, name))

    async def _resolve_and_apply(self, zeroconf: Any, info: AsyncServiceInfo, name: str) -> None:
        """Async resolve path for cache misses."""
        try:
            resolved = await info.async_request(zeroconf, timeout=_RESOLVE_TIMEOUT_MS)
        except Exception:
            _LOGGER.debug("Resolve failed for %s", name, exc_info=True)
            return
        if not resolved:
            return
        self._upsert_host(name, info)

    def _upsert_host(self, name: str, info: AsyncServiceInfo) -> None:
        """Replace the row keyed on *name* and fire ``REMOTE_BUILD_HOST_ADDED``.

        Drops entries whose ``(server, port)`` matches our own
        advertise — the instance-name filter handles the common
        case, but a rename-on-conflict bounce can leave the
        captured name stale.
        """
        peer = peer_from_service_info(name, info)
        if self._is_self_endpoint(peer.hostname, peer.port):
            return
        self._peers[name] = peer
        self._db.bus.fire(EventType.REMOTE_BUILD_HOST_ADDED, peer.to_dict())
        # mDNS auto-rebind: if this broadcast's pin matches a
        # stored pairing whose ``(host, port)`` differs, the
        # probe-then-rebind background task verifies the new
        # endpoint really is our paired receiver before mutating.
        self._maybe_schedule_rebind_probe(peer)

    def _is_self_endpoint(self, hostname: str, port: int) -> bool:
        """Return True when *(hostname, port)* matches our published advertise.

        Reads the live ``service_target_endpoint`` off the
        :class:`DashboardAdvertiser` rather than a captured-at-start
        value so a post-start register / re-register isn't missed.
        Hostname comparison goes through
        :func:`normalize_hostname` so the resolved peer hostname
        and the advertiser's published target compare equal
        regardless of trailing-dot / case.
        """
        advertiser = self._db._dashboard_advertiser
        if advertiser is None:
            return False
        endpoint = advertiser.service_target_endpoint
        if endpoint is None:
            return False
        own_host, own_port = endpoint
        return endpoints_equal(hostname, port, own_host, own_port)

    def _fire_host_removed(self, name: str) -> None:
        """Fire ``REMOTE_BUILD_HOST_REMOVED`` for *name*."""
        payload: RemoteBuildHostRemovedData = {"name": name}
        self._db.bus.fire(EventType.REMOTE_BUILD_HOST_REMOVED, payload)

    def _track_task(
        self, coro: Coroutine[Any, Any, None], *, name: str | None = None
    ) -> asyncio.Task[None]:
        """Schedule *coro* and hold a strong ref in :attr:`_tasks` until it settles.

        Distinct from :meth:`DeviceBuilder.create_background_task`
        — this set is drained separately by :meth:`stop` for
        ordered subsystem teardown.
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_cleanup_loop(self) -> None:
        """Sweep cold remote-build subtrees every ``_CLEANUP_SWEEP_INTERVAL_SECONDS``.

        Sleeps before the first cycle — a fresh install has no
        subtrees to reclaim and the TTL is 24h. Per-cycle
        failures are logged and the loop continues; cancel via
        :meth:`stop` settles cleanly through the sleep.
        """
        config_dir = self._db.settings.config_dir
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_CLEANUP_SWEEP_INTERVAL_SECONDS)
            try:
                # Re-check firmware narrows the type for mypy and
                # survives a future spawn/start decoupling.
                firmware = self._db.firmware
                if firmware is None:
                    continue
                settings = await self._load_settings_async()
                in_flight_keys = frozenset(
                    rbp
                    for job in firmware.active_remote_peer_jobs()
                    if (rbp := parse_from_configuration(job.configuration)) is not None
                )
                deleted = await loop.run_in_executor(
                    None,
                    partial(
                        sweep_remote_builds,
                        config_dir,
                        ttl_seconds=settings.cleanup_ttl_seconds,
                        in_flight_keys=in_flight_keys,
                    ),
                )
                if deleted:
                    _LOGGER.info("remote-build cleanup: swept %d cold subtree(s)", deleted)
            except Exception:
                _LOGGER.exception("remote-build cleanup sweep failed")

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
        """Probe + identity-verify a candidate endpoint without mutating state.

        Shared by the mDNS auto-rebind path and the user-driven
        endpoint edit; each caller maps the typed outcome onto
        its own surface. One ``intent="preview"`` round-trip
        covers three checks: reachability (TCP + handshake),
        identity (pubkey vs stored pin), and race-safety
        (captured pairing object still in the dict, still
        APPROVED).
        """
        assert self._offloader_peer_link_priv is not None
        try:
            observed_pin = await peer_link_preview_pair(
                hostname=new_hostname,
                port=new_port,
                identity_priv=self._offloader_peer_link_priv,
                resolver=self._peer_link_resolver,
            )
        except PeerLinkClientError as exc:
            return RebindProbeResult(RebindProbeOutcome.UNREACHABLE, transport_error=exc)
        if observed_pin != pairing.pin_sha256:
            return RebindProbeResult(RebindProbeOutcome.PIN_MISMATCH, observed_pin=observed_pin)
        current = self._pairings.get(pairing.pin_sha256)
        if current is not pairing:
            return RebindProbeResult(RebindProbeOutcome.PAIRING_REPLACED)
        if current.status is not PeerStatus.APPROVED:
            return RebindProbeResult(RebindProbeOutcome.STATUS_CHANGED)
        return RebindProbeResult(RebindProbeOutcome.OK)

    def _commit_endpoint_rebind(self, pairing: StoredPairing, *, hostname: str, port: int) -> None:
        """Mutate *pairing* to (*hostname*, *port*) and run the rebind epilogue.

        Clears the per-pin probe cooldown — a successful rebind
        means the next mDNS Updated should probe immediately.
        Caller owns the probe + identity verify; no checks here.
        """
        pairing.receiver_hostname = hostname
        pairing.receiver_port = port
        self._schedule_pairings_save()
        self._respawn_peer_link_at_new_endpoint(pairing)
        self._rebind_probe_until.pop(pairing.pin_sha256, None)

    def _respawn_peer_link_at_new_endpoint(self, pairing: StoredPairing) -> None:
        """Cancel + respawn the peer-link client and fire the rebind event.

        The caller has already mutated *pairing*'s
        hostname/port; this is the shared epilogue.
        """
        self._cancel_peer_link_client(pairing.pin_sha256)
        self._spawn_peer_link_client(pairing)
        self._fire_offloader_pair_endpoint_rebound(
            pin_sha256=pairing.pin_sha256,
            receiver_hostname=pairing.receiver_hostname,
            receiver_port=pairing.receiver_port,
        )

    # ------------------------------------------------------------------
    # mDNS auto-rebind
    # ------------------------------------------------------------------

    def _maybe_schedule_rebind_probe(self, peer: RemoteBuildPeer) -> None:
        """Spawn a probe-and-rebind task if *peer* is a known pin at a new endpoint.

        Called from :meth:`_upsert_host` on every resolved
        broadcast. Cheap early-returns dominate (most discoveries
        are unpaired peers or steady-state re-announces); only a
        rare hostname / port change for an APPROVED pairing
        spawns a probe task. The probe slot is rate-limited via
        :attr:`_rebind_probe_until` so a burst of zeroconf
        Updated callbacks or a permanently-unreachable host both
        collapse to one probe per
        :data:`_REBIND_PROBE_COOLDOWN_SECONDS`.
        """
        pin = peer.pin_sha256
        new_port = peer.remote_build_port
        if not pin or new_port == 0:
            return
        pairing = self._pairings.get(pin)
        if pairing is None or pairing.status is not PeerStatus.APPROVED:
            return
        new_hostname = normalize_hostname(peer.hostname)
        if endpoints_equal(
            pairing.receiver_hostname, pairing.receiver_port, new_hostname, new_port
        ):
            return
        if self._offloader_peer_link_priv is None:
            return
        now = time.monotonic()
        if self._rebind_probe_until.get(pin, 0.0) > now:
            return
        self._rebind_probe_until[pin] = now + _REBIND_PROBE_COOLDOWN_SECONDS
        self._track_task(
            self._probe_and_rebind_endpoint(
                pairing=pairing, new_hostname=new_hostname, new_port=new_port
            ),
            name=f"rebind-probe-{pin[:8]}",
        )

    async def _probe_and_rebind_endpoint(
        self, *, pairing: StoredPairing, new_hostname: str, new_port: int
    ) -> None:
        """Probe the candidate endpoint; rebind the pairing iff the pin still matches.

        One ``preview`` round-trip checks reachability + identity
        in one call. ``preview`` bypasses the pairing window so a
        quiet receiver doesn't deadlock the rebind path. On a
        successful match, mutate :class:`StoredPairing` in place,
        schedule the debounced save, cancel + respawn the
        peer-link client at the new coordinates, fire
        :attr:`EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND`, and
        clear the cooldown so a future move is probed
        immediately. Failure paths leave the cooldown in place.
        """
        pin = pairing.pin_sha256
        with self._clear_cooldown_on_unexpected_exit(pin):
            result = await self._probe_pairing_endpoint(
                pairing=pairing, new_hostname=new_hostname, new_port=new_port
            )
            if result.outcome is RebindProbeOutcome.UNREACHABLE:
                # Pass the captured ``PeerLinkClientError`` as
                # ``exc_info=`` so the debug log carries the
                # full traceback for diagnosing handshake /
                # connect failures in the field — same shape
                # the inline ``except`` block had before this
                # path was factored into ``_probe_pairing_endpoint``.
                _LOGGER.debug(
                    "rebind probe %s -> %s:%d failed (unreachable / handshake error)",
                    pin,
                    new_hostname,
                    new_port,
                    exc_info=result.transport_error,
                )
                return
            if result.outcome is RebindProbeOutcome.PIN_MISMATCH:
                _LOGGER.warning(
                    "rebind probe %s -> %s:%d observed pin %s; ignoring (spoof or rotation)",
                    pin,
                    new_hostname,
                    new_port,
                    result.observed_pin,
                )
                return
            if result.outcome is not RebindProbeOutcome.OK:
                # PAIRING_REPLACED / STATUS_CHANGED — silent skip;
                # cooldown stays in place so a burst of mDNS
                # Updated callbacks doesn't re-fire the probe
                # against state that's already moved on.
                return
            self._commit_endpoint_rebind(pairing, hostname=new_hostname, port=new_port)
            _LOGGER.info("rebound pairing %s to %s:%d", pin, new_hostname, new_port)

    @contextmanager
    def _clear_cooldown_on_unexpected_exit(self, pin: str) -> Iterator[None]:
        """Pop *pin* from ``_rebind_probe_until`` iff the wrapped block raises.

        Graceful failure paths inside the probe (unreachable
        host, pin mismatch, mid-probe re-pair) preserve the
        cooldown entry to throttle retries. Cancellation /
        unexpected exceptions shouldn't lock the pin out of
        future legitimate rebind attempts, so on any escaped
        exception we drop the entry before the exception
        propagates.
        """
        try:
            yield
        except BaseException:
            self._rebind_probe_until.pop(pin, None)
            raise

    def _fire_offloader_pair_endpoint_rebound(
        self,
        *,
        pin_sha256: str,
        receiver_hostname: str,
        receiver_port: int,
    ) -> None:
        """Fire ``OFFLOADER_PAIR_ENDPOINT_REBOUND`` after a successful rebind."""
        payload: OffloaderPairEndpointReboundData = {
            "pin_sha256": pin_sha256,
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND, payload)

    def hosts_snapshot(self) -> list[RemoteBuildPeer]:
        """Return the current mDNS-discovered hosts for ``subscribe_events`` seeding."""
        return list(self._peers.values())

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------

    @api_command("remote_build/get_settings")
    async def get_settings(self, **kwargs: Any) -> RemoteBuildSettingsView:
        """Return the receiver-side remote-build settings (wire view)."""
        return self._to_view(await self._load_settings_async())

    def _to_view(self, settings: RemoteBuildSettings) -> RemoteBuildSettingsView:
        """Project receiver settings to wire view, merging in-memory peers.

        The peer list is RAM-canonical: PENDING entries live in
        ``self._pending_peers`` for the active pairing window's
        lifetime (never hit disk) and APPROVED entries live in
        ``self._approved_peers`` / its per-file ``Store``.
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
        ``_peer_link_sessions``; PENDING always
        ``connected=False`` since the peer-link dispatch
        refuses non-APPROVED rows.
        """
        sessions = self._peer_link_sessions
        return [
            peer_summary(p, status=PeerStatus.PENDING, connected=False)
            for p in self._pending_peers.values()
        ] + [
            peer_summary(p, status=PeerStatus.APPROVED, connected=p.dashboard_id in sessions)
            for p in self._approved_peers.values()
        ]

    def approved_peer_label(self, dashboard_id: str) -> str:
        """Return the APPROVED peer's display label, or ``""`` if not found."""
        peer = self._approved_peers.get(dashboard_id)
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
        """Return the live :class:`PeerLinkClient` for *pin_sha256*, raising on miss.

        ``NOT_FOUND`` for a missing pairing; ``PRECONDITION_FAILED``
        for any of the not-ready states (PENDING, client not
        spawned, orphaned, mid-reconnect) — all four fold into
        one raise since the user's recovery is the same (wait +
        retry); the distinguishing reason rides in the log line.
        *label* names the calling op in the error message.
        """
        pairing = self._pairings.get(pin_sha256)
        if pairing is None:
            msg = f"{label}: no pairing for pin_sha256={pin_sha256!r}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        if pairing.status is not PeerStatus.APPROVED:
            reason = f"status is {pairing.status.value!r}, not APPROVED"
        elif (handle := self._peer_link_clients.get(pin_sha256)) is None:
            reason = "client not yet spawned"
        elif handle.task.done():
            reason = "client orphaned (pin mismatch / superseded)"
        elif not handle.client.is_session_open:
            reason = "session not connected (mid-reconnect / receiver offline)"
        else:
            return handle.client
        msg = f"{label}: peer-link to {pairing.label!r} not ready ({reason})"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)

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
        """Spawn the long-lived peer-link client for *pairing*.

        Idempotent on already-running clients — returns early if
        a client for the row's ``pin_sha256`` is still alive.
        Skips if the offloader-side identities haven't been
        loaded yet (start order: identities load before any
        spawn). Skips if the bus isn't wired (e.g. a unit test
        path).
        """
        if (
            self._offloader_dashboard_id is None
            or self._offloader_peer_link_priv is None
            or self._db.bus is None
        ):
            return
        key = pairing.pin_sha256
        existing = self._peer_link_clients.get(key)
        if existing is not None and not existing.task.done():
            return
        client = PeerLinkClient(
            receiver_hostname=pairing.receiver_hostname,
            receiver_port=pairing.receiver_port,
            identity_priv=self._offloader_peer_link_priv,
            dashboard_id=self._offloader_dashboard_id,
            # Pin the receiver's static pubkey from the
            # OOB-verified pair flow so the long-lived peer-link
            # handshake fails fast on identity drift instead of
            # admitting an attacker with their own keypair to
            # the application channel.
            pinned_static_x25519_pub=pairing.static_x25519_pub,
            pin_sha256=pairing.pin_sha256,
            receiver_label=pairing.label,
            bus=self._db.bus,
            resolver=self._peer_link_resolver,
        )
        task = asyncio.create_task(
            client.run(),
            name=f"peer-link-client-{pairing.receiver_hostname}:{pairing.receiver_port}",
        )
        self._peer_link_clients[key] = PeerLinkClientHandle(client=client, task=task)

    def _cancel_peer_link_client(self, pin_sha256: str) -> None:
        """Cancel the peer-link client for *pin_sha256*. No-op if none running."""
        handle = self._peer_link_clients.pop(pin_sha256, None)
        if handle is not None and not handle.task.done():
            handle.task.cancel()

    def _sweep_stale_pairings_at_endpoint(
        self, hostname: str, port: int, *, keep_pin_sha256: str
    ) -> None:
        """Drop any pairing or alert at ``(hostname, port)`` whose pin isn't *keep_pin_sha256*.

        Cleans up after a re-pair against the same endpoint
        under a fresh pin (receiver rotated identity, or a
        different receiver took the hostname). Without the
        sweep the old row + listener task + alert would leak
        under pin-keying.

        Walks both ``_pairings`` and ``_offloader_alerts``
        because an alert can outlive its pairing on the
        pin-drift branch. Snapshots to lists before iterating
        to avoid mutate-during-iteration.
        """
        for stale_pin, pairing in list(self._pairings.items()):
            if stale_pin == keep_pin_sha256:
                continue
            if pairing.receiver_hostname != hostname or pairing.receiver_port != port:
                continue
            self._pairings.pop(stale_pin, None)
            self._cancel_pair_status_listener(stale_pin)
            self._cancel_peer_link_client(stale_pin)
            self._peer_queue_status.pop(stale_pin, None)
            self._open_peer_links.discard(stale_pin)
        # Alerts can outlive pairings — sweep them in a second
        # pass keyed on the alert's stored ``receiver_hostname``
        # / ``receiver_port`` (also walks the pin-keyed dict so
        # an alert under the keep_pin_sha256 stays put if the
        # user is re-confirming the same identity).
        for stale_pin, alert in list(self._offloader_alerts.items()):
            if stale_pin == keep_pin_sha256:
                continue
            if alert["receiver_hostname"] != hostname or alert["receiver_port"] != port:
                continue
            self._dismiss_offloader_alert(stale_pin, hostname, port)

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

    @api_command("remote_build/get_identity")
    async def get_identity(self, **kwargs: Any) -> IdentityView:
        """Return this dashboard's stable identity (id + pin + versions + bind state).

        The X25519 private key is never returned; only
        ``pin_sha256`` (the fingerprint mDNS broadcasts and
        offloaders pin against).
        """
        loop = asyncio.get_running_loop()
        identity = await loop.run_in_executor(
            None, get_or_create_identity, self._db.settings.config_dir
        )
        return identity_view(identity, listener_bound=self._db.is_remote_build_listener_bound)

    @api_command("remote_build/rotate_identity")
    async def rotate_identity(self, **kwargs: Any) -> IdentityView:
        """
        Mint a fresh X25519 peer-link keypair, replacing whatever's on disk.

        Forces every paired offloader to re-pair — peers pinned
        on the old ``pin_sha256`` see a fingerprint mismatch on
        the next handshake. ``dashboard_id`` is preserved.

        Side effects when remote-build is currently bound:
        listener torn down + rebuilt with the fresh key,
        ``pin_sha256`` re-advertised in mDNS, rebuild fail-softs
        (``listener_bound=False`` in the response).
        :attr:`EventType.REMOTE_BUILD_IDENTITY_ROTATED` fires
        regardless of bind state so subscribers can refresh
        cached pins without polling.

        Concurrent calls return ``ALREADY_EXISTS`` — two
        rotations racing would each tear down + rebuild the
        listener; back-to-back is almost always an accidental
        double-click.
        """
        # Check+set is atomic on the single asyncio loop.
        if self._rotation_in_flight:
            msg = "remote_build: an identity rotation is already in progress"
            raise CommandError(ErrorCode.ALREADY_EXISTS, msg)
        self._rotation_in_flight = True
        try:
            loop = asyncio.get_running_loop()
            identity = await loop.run_in_executor(
                None, _dashboard_identity_helper.rotate_identity, self._db.settings.config_dir
            )
            listener_bound = await self._db.reload_remote_build_identity(
                pin_sha256=identity.pin_sha256,
            )
            self._db.bus.fire(
                EventType.REMOTE_BUILD_IDENTITY_ROTATED,
                RemoteBuildIdentityRotatedData(
                    dashboard_id=identity.dashboard_id,
                    pin_sha256=identity.pin_sha256,
                ),
            )
            return identity_view(identity, listener_bound=listener_bound)
        finally:
            self._rotation_in_flight = False

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
        RAM-canonical ``_approved_peers`` dict, schedules a
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

        pending = self._pending_peers.pop(clean_id, None)
        if pending is None:
            # Differentiate "already approved" from "never existed"
            # so the frontend can decide whether to refresh or
            # surface an error. Both reads short-circuit through
            # RAM — no disk I/O.
            if clean_id in self._approved_peers:
                msg = f"peer is already approved: {clean_id}"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            msg = f"no pending peer with dashboard_id: {clean_id}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)

        self._approved_peers[clean_id] = pending
        self._peers_store.async_delay_save(
            self._serialize_peers, delay=_PAIRINGS_SAVE_DELAY_SECONDS
        )
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
        * Removing an APPROVED row from ``_approved_peers``
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
        if self._pending_peers.pop(clean_id, None) is not None:
            self._fire_pair_status_changed(clean_id, "removed")
            return await self._current_settings_view()

        if self._approved_peers.pop(clean_id, None) is None:
            msg = f"no peer with dashboard_id: {clean_id}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        self._peers_store.async_delay_save(
            self._serialize_peers, delay=_PAIRINGS_SAVE_DELAY_SECONDS
        )
        self._fire_pair_status_changed(clean_id, "removed")
        return await self._current_settings_view()

    def _serialize_peers(self) -> ReceiverPeers:
        """Build the on-disk peers shape from the in-RAM ``_approved_peers`` dict."""
        return ReceiverPeers(peers=list(self._approved_peers.values()))

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
        approved_peer = self._approved_peers.get(dashboard_id)
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
        existing = self._pending_peers.get(dashboard_id)
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
        self._pending_peers[dashboard_id] = StoredPeer(
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
        # (the APPROVED list moved off disk into ``_approved_peers``
        # at startup).
        pending = self._pending_peers.get(dashboard_id)
        if pending is not None:
            if pending.pin_sha256 != pin_sha256:
                return IntentResponse.REJECTED
            return IntentResponse.PENDING
        peer = self._approved_peers.get(dashboard_id)
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
            self._pairing_window_clients[client] = time.monotonic()
        else:
            self._pairing_window_clients.pop(client, None)
        self._reschedule_pairing_window_close()
        is_open = bool(self._pairing_window_clients)

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
        return bool(self._pairing_window_clients)

    def _pairing_window_remaining(self) -> float | None:
        """Seconds until the latest-extend deadline, or ``None`` if closed."""
        self._prune_stale_pairing_window_clients()
        if not self._pairing_window_clients:
            return None
        latest_extend = max(self._pairing_window_clients.values())
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
        if not self._pairing_window_clients:
            return
        cutoff = time.monotonic() - _PAIRING_WINDOW_DURATION_SECONDS
        self._pairing_window_clients = {
            client: extended_at
            for client, extended_at in self._pairing_window_clients.items()
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
        if self._pairing_window_handle is not None:
            self._pairing_window_handle.cancel()
            self._pairing_window_handle = None
        remaining = self._pairing_window_remaining()
        if remaining is None:
            return
        loop = asyncio.get_running_loop()
        self._pairing_window_handle = loop.call_later(remaining, self._on_pairing_window_deadline)

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
        self._pairing_window_handle = None
        self._pairing_window_clients.clear()
        self._fire_pairing_window_changed()
        self._clear_pending_peers_on_window_close()

    def _clear_pending_peers_on_window_close(self) -> None:
        """Drop every PENDING peer + fire ``status="removed"`` for each.

        Wakes any in-flight ``lookup_peer_for_status`` long-poll
        so its offloader sees REJECTED.
        """
        if not self._pending_peers:
            return
        cleared = list(self._pending_peers)
        self._pending_peers.clear()
        for dashboard_id in cleared:
            self._fire_pair_status_changed(dashboard_id, "removed")
