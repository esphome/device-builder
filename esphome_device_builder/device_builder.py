"""ESPHome Device Builder — core application singleton.

The DeviceBuilder class is the main entry point. It owns controllers,
the event bus, and the aiohttp web application. Device state lives in
the DevicesController, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any

from aiohttp import web
from esphome.const import __version__ as esphome_version

from ._remote_build_lifecycle import RemoteBuildLifecycle
from .api.legacy import create_legacy_routes
from .api.ws import create_ws_routes, init_ws_app
from .constants import __version__ as server_version
from .controllers.auth import AuthController
from .controllers.automations import AutomationsController
from .controllers.boards import BoardCatalog
from .controllers.components import ComponentCatalog
from .controllers.config import (
    ConfigController,
    DashboardSettings,
)
from .controllers.devices import DevicesController
from .controllers.editor import EditorController
from .controllers.firmware import FirmwareController
from .controllers.firmware.download import http_download as firmware_http_download
from .controllers.labels import LabelsController
from .controllers.onboarding import OnboardingController
from .controllers.remote_build import OffloaderController, ReceiverController
from .controllers.version_history import VersionHistoryController
from .helpers.api import CommandHandler, collect_api_commands
from .helpers.async_ import create_eager_task, drain_tasks
from .helpers.auth import HASHED_FILENAME_RE, auth_middleware, ingress_peer_guard
from .helpers.dashboard_advertise import DashboardAdvertiser
from .helpers.dashboard_identity import get_or_create_identity as get_or_create_dashboard_identity
from .helpers.event_bus import Event, EventBus, StreamControls, stream_events
from .helpers.json import cors_middleware, json_response
from .helpers.network_interfaces import ensure_single_host_for_ephemeral_port, resolve_bind_host
from .helpers.peer_link_identity import PeerLinkIdentityStore
from .helpers.secrets_state import write_secrets_locked
from .helpers.startup_timing import StartupTimer
from .helpers.subscriber_presence import SubscriberPresence
from .models import EventType

_LOGGER = logging.getLogger(__name__)

# How often ``_run_background`` re-runs ``DevicesController.poll``
# while at least one WS client is subscribed. Bounded above by how
# stale a "user dropped a YAML in via SSH" change is allowed to look
# in the dashboard's device list; bounded below by the cost of the
# directory-walk + per-file stat the poll triggers via
# ``DeviceScanner.scan``. The ICMP ping sweep already runs on a
# similar cadence — keep the two in the same ballpark so a fleet's
# steady-state idle CPU doesn't spike on either alone.
_BACKGROUND_POLL_INTERVAL_SECONDS = 5

# Upper bound on how long ``web.run_app`` waits for in-flight HTTP
# request handlers to finish after a SIGTERM before invoking
# ``on_cleanup`` and exiting. aiohttp's default is 60s, which sets
# the worst-case SIGTERM-to-exit latency the desktop wrapper sees;
# our ``close_active_websockets`` ``on_shutdown`` handler already
# unwinds every long-lived WS handler, so the only thing this
# timeout still bounds is a freshly-arrived HTTP request that was
# mid-handler when the signal landed. 5s is comfortably above any
# normal handler latency in this codebase and tight enough that a
# bug in a slow handler can't silently extend shutdown to a minute.
_SHUTDOWN_TIMEOUT_SECONDS = 5.0

# Cache policy for the SPA shell:
#   - ``index.html`` and any non-hashed top-level file: must always
#     revalidate so a re-deployed wheel doesn't get masked by a
#     stale browser cache.
#   - Hashed bundles (``app.<hash>.js``, ``vendors.<hash>.js``,
#     license sidecars) are content-addressed — the filename changes
#     on every rebuild, so they're safe to cache forever.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}
_IMMUTABLE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}

# Path extensions that should NEVER fall back to ``index.html``. The
# frontend bundle's entry script is emitted with a relative ``src``
# (rspack ``publicPath: "auto"`` for ingress / reverse-proxy
# subpath support), so a hard-reload of a deep SPA URL like
# ``/device/<id>`` resolves the script as ``/device/app.<hash>.js``.
# Falling back to ``index.html`` for that path would let the
# browser parse HTML as JavaScript and white-screen on
# "Unexpected token '<'". Returning 404 instead keeps the failure
# mode legible — by then the ``<base>`` injection should have
# steered the script's URL to the deployment root anyway, so
# this is the belt to that suspenders.
_ASSET_EXTENSIONS = frozenset(
    {".js", ".css", ".map", ".woff", ".woff2", ".ttf", ".otf", ".ico", ".png"}
)

# Placeholder the frontend's ``index.html`` carries verbatim; the
# backend renders it per-request with the deployment-base prefix.
# Sentinel chosen to be HTML-attribute-safe and unambiguous in a
# diff so a partial replacement is loud, not silent.
_BASE_HREF_PLACEHOLDER = "__ESPHOME_BASE_HREF__"

# Headers the rendered shell varies on. Both reverse proxies and
# the HA add-on ingress layer announce a stripped path prefix —
# nginx-style proxies via ``X-Forwarded-Prefix`` and HA core's
# ingress proxy via ``X-Ingress-Path`` (set in
# ``homeassistant/components/hassio/ingress.py:_init_header``,
# passed through unchanged by the supervisor proxy). The rendered
# ``<base href>`` differs per source, so without ``Vary`` an
# intermediary cache could serve the wrong-prefix shell to a
# different client.
_BASE_HREF_VARY = "X-Ingress-Path, X-Forwarded-Prefix"


def _resolve_base_href(request: web.Request, *, tail: str = "") -> str:
    """Pick the ``<base href>`` for *request*'s deployment.

    Strict precedence — the first source that yields a non-empty
    value wins, the rest are skipped:

    1. ``X-Ingress-Path`` header — set by Home Assistant core's
       ingress proxy to the per-token ingress prefix
       (``/api/hassio_ingress/<token>``, no trailing slash). The
       supervisor's ingress proxy passes it through unchanged, so
       the add-on sees the canonical prefix the browser used.
       This is the dominant production deployment shape, so it
       wins over ``X-Forwarded-Prefix`` in the unlikely case both
       headers arrive on the same request.
    2. ``X-Forwarded-Prefix`` header — the standardised reverse-
       proxy signal for non-HA setups (nginx subpath, traefik,
       caddy). Production deployments only set one of the two
       headers in practice; this branch is for the non-HA path.
    3. ``request.path`` minus the matched SPA-fallback tail —
       lets a direct deploy at ``/`` recover the (empty) prefix
       without the operator having to set a header. Caller passes
       the aiohttp ``match_info`` tail in directly so the backend
       doesn't track the SPA route table.

    Always returns a path with exactly one leading and one
    trailing slash. Collapses runs of slashes on either end so
    ``X-Forwarded-Prefix: //evil.com`` can't yield a
    protocol-relative base, and ``/dashboard//`` can't produce
    ``//`` runs in resolved asset URLs.
    """
    ingress = request.headers.get("X-Ingress-Path", "").strip()
    forwarded = request.headers.get("X-Forwarded-Prefix", "").strip()
    if ingress:
        base = ingress
    elif forwarded:
        base = forwarded
    elif tail and request.path.endswith(tail):
        # Slice the matched SPA tail off the request path to get
        # the mount-point prefix. No SPA-route knowledge needed in
        # the backend; the aiohttp router already matched the tail
        # and we trust its match_info.
        base = request.path[: -len(tail)] or "/"
    else:
        base = request.path
    # Normalise to exactly one leading + trailing slash. ``strip``
    # collapses both ``//evil.com`` injection attempts (back to a
    # single on-origin slash) and ``/dashboard//`` runs (so the
    # rendered ``<base href>`` doesn't produce ``//`` runs in
    # resolved asset URLs); the leading + trailing slashes are then
    # re-added.
    normalized = base.strip("/")
    return f"/{normalized}/" if normalized else "/"


async def _handle_version(_request: web.Request) -> web.Response:
    """Return the esphome version as JSON for the Docker HEALTHCHECK."""
    return json_response({"version": esphome_version})


# Worker-thread budget for the default ``ThreadPoolExecutor``. asyncio's
# default is ``min(32, os.cpu_count() + 4)`` — too tight for the
# dashboard's I/O-bound workload (DNS resolves on every ping sweep,
# scanner stats, YAML parses, MQTT TCP connect) once the device count
# crosses ~30. 64 leaves comfortable headroom on a saturated sweep
# without fanning out so wide that the OS thread table balloons. Keep
# this as a module-level constant so the value is one place to audit
# and the test suite's pin-down assertion can reference it.
_EXECUTOR_MAX_WORKERS = 64

# Event types broadcast to every ``subscribe_events`` client. Two are held
# back: ``DEVICE_REACHABILITY`` fires per-signal for every device (60+/min
# under fleet load) and rides the per-device ``subscribe_reachability``
# stream instead, and ``DEVICE_YAML_UPDATED`` is an internal version-history
# signal (clients already get ``DEVICE_UPDATED`` for the row). Computed once.
_INTERNAL_ONLY_EVENTS = frozenset({EventType.DEVICE_REACHABILITY, EventType.DEVICE_YAML_UPDATED})
_BROADCAST_EVENT_TYPES = [et for et in EventType if et not in _INTERNAL_ONLY_EVENTS]


class DeviceBuilder:
    """Core application singleton.

    Owns controllers, event bus, command registry, and web app.
    All device state lives in DevicesController.
    """

    def __init__(
        self, settings: DashboardSettings, *, startup_timer: StartupTimer | None = None
    ) -> None:
        """Initialize the Device Builder."""
        self.settings = settings
        self._startup_timer = startup_timer
        self.bus = EventBus()
        self.peer_link_identity_store = PeerLinkIdentityStore(settings.config_dir)
        # Reference-counted "is anyone watching the dashboard?" gate.
        # The ``subscribe_events`` body wraps itself in
        # ``presence.subscriber()`` so consumers — currently the
        # state monitor's ICMP ping loop — can park while the gate
        # is closed and resume on the 0→1 transition. Mirrors the
        # legacy dashboard's ``ping_request`` / ``self._subscribers``
        # pair so a quiet network with no observers generates no
        # ICMP traffic.
        self.subscriber_presence = SubscriberPresence()
        # Serialises every secrets.yaml read-modify-write across
        # controllers so concurrent per-key mutations can't lose updates.
        self.secrets_write_lock = asyncio.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        # Held so ``stop()`` can shut the pool down explicitly. Created
        # eagerly here (not in start()) so a test or caller that probes
        # the executor before lifecycle starts still sees the right
        # one. ``ThreadPoolExecutor`` only spawns threads on demand.
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=_EXECUTOR_MAX_WORKERS, thread_name_prefix="dashboard"
        )

        # Controllers — populated in start()
        self.auth: AuthController | None = None
        self.boards: BoardCatalog | None = None
        self.components: ComponentCatalog | None = None
        self.config: ConfigController | None = None
        self.devices: DevicesController | None = None
        self.automations: AutomationsController | None = None
        self.firmware: FirmwareController | None = None
        self.editor: EditorController | None = None
        self.labels: LabelsController | None = None
        self.onboarding: OnboardingController | None = None
        self.remote_build_offloader: OffloaderController | None = None
        self.remote_build_receiver: ReceiverController | None = None
        self.version_history: VersionHistoryController | None = None

        # mDNS advertise — populated in start() once we know zeroconf
        # is up. Optional: a zeroconf-bind failure leaves this None
        # and dashboard discovery just doesn't happen for this
        # process (device discovery, the load-bearing mDNS feature,
        # has the same fail-soft contract).
        self._dashboard_advertiser: DashboardAdvertiser | None = None

        # Command registry — populated from controllers
        self.command_handlers: dict[str, CommandHandler] = {}

        # Background tasks
        self._background_tasks: set[asyncio.Task] = set()
        self._bg_task: asyncio.Task | None = None

        # Latches the one-time network teardown so it can run early (in the
        # aiohttp ``on_shutdown`` hook) and stop() doesn't repeat it.
        self._network_stopped = False

        self._ingress_runner: web.AppRunner | None = None
        # Remote-build peer-link listener lifecycle (issue #106) —
        # bind / teardown / rebuild of the Noise-XX receiver site and
        # its mDNS advertise. Owns the bound runner and the lifecycle
        # lock; driven through its public methods below.
        self._remote_build_lifecycle = RemoteBuildLifecycle(self)

    @property
    def dashboard_advertiser(self) -> DashboardAdvertiser | None:
        """The dashboard mDNS advertiser, or ``None`` before start() / when zeroconf is down."""
        return self._dashboard_advertiser

    @property
    def is_remote_build_listener_bound(self) -> bool:
        """True iff the remote-build peer-link Noise WS listener is currently bound."""
        return self._remote_build_lifecycle.is_listener_bound

    async def apply_remote_build_enabled(self) -> bool:
        """Converge the peer-link listener to the on-disk ``enabled`` flag."""
        return await self._remote_build_lifecycle.apply_enabled()

    async def reload_remote_build_identity(self) -> bool:
        """Rebuild the peer-link listener after an X25519 identity rotation."""
        return await self._remote_build_lifecycle.reload_identity()

    def invalidate_editor_cache(self) -> None:
        """Drop the editor's validate cache after a config-dir write; no-op pre-start."""
        if self.editor is not None:
            self.editor.invalidate_cache()

    async def write_secrets_locked[T](self, fn: Callable[..., T], *args: Any) -> T:
        """
        Run a ``secrets.yaml`` mutator under the shared lock, then drop the editor cache.

        The single funnel for secrets writes: routing every writer through here
        couples the cache invalidation to the write, so a new secrets path can't
        forget that an open editor's ``!secret`` lint just went stale.
        """
        result = await write_secrets_locked(self.secrets_write_lock, fn, *args)
        self.invalidate_editor_cache()
        return result

    def _install_default_executor(self) -> None:
        """Register the dashboard's executor as the loop's default.

        Extracted so the unit test can drive the same registration
        path the production ``start()`` flow uses, instead of
        re-implementing ``loop.set_default_executor(self._executor)``
        and trivially passing even when ``start()`` stopped doing it.
        Raises explicitly (rather than ``assert``) because asserts are
        stripped under ``python -O`` and a missing loop / closed pool
        here is a real bug we'd rather surface as ``RuntimeError``
        than as a downstream ``AttributeError`` in the loop's guts.
        """
        if self.loop is None:
            msg = "DeviceBuilder.loop is not set; call start() first"
            raise RuntimeError(msg)
        if self._executor is None:
            msg = "DeviceBuilder._executor was already shut down"
            raise RuntimeError(msg)
        self.loop.set_default_executor(self._executor)

    async def start(self) -> None:
        """Start the application — load catalogs, initialize controllers."""
        self.loop = asyncio.get_running_loop()
        # Re-arm the network-teardown latch so a restart-in-place teardown runs.
        self._network_stopped = False
        # Pool itself was constructed in ``__init__`` (so callers
        # probing ``self._executor`` pre-start see the right value);
        # here we just register it as the loop's default. See
        # ``_EXECUTOR_MAX_WORKERS`` for the why behind the pool size.
        self._install_default_executor()

        # Initialize controllers
        self.auth = AuthController(self)
        self.boards = BoardCatalog()
        self.boards.load()
        self.components = ComponentCatalog(self)
        self.components.load()
        self.config = ConfigController(self)
        self.devices = DevicesController(self)
        self.automations = AutomationsController(self)
        self.firmware = FirmwareController(self)
        self.editor = EditorController(self)
        self.labels = LabelsController(self)
        self.onboarding = OnboardingController(self)
        self.remote_build_offloader = OffloaderController(self)
        self.remote_build_receiver = ReceiverController(self)
        self.version_history = VersionHistoryController(self)
        # Seed the RAM-canonical preferences (and migrate them out of the shared
        # sidecar on first run) before onboarding reads or mutates them.
        await self.config.async_load()
        # Default pre-existing installs to the YAML experience before
        # any onboarding command can be served.
        await self.onboarding.migrate_preexisting_install()
        await self.devices.start()
        await self.firmware.start()
        await self.editor.start()
        await self.version_history.start()

        # Advertise this dashboard on mDNS so peer dashboards (and
        # the future ESPHome Desktop welcome screen) can discover it.
        # Reuses the state monitor's zeroconf instance so the
        # responder count stays at one per process.
        #
        # Advertised in HA addon mode too: the addon runs with host
        # networking, so the announce carries the host's LAN IP (the
        # ``hassio`` Supervisor bridge is filtered out in
        # ``_local_addresses``) and reaches peers. The advertise is
        # discovery-only — it tells the Desktop a builder exists; it
        # does not imply a peer-link receiver is bound (that stays
        # default-off on the addon, see ``maybe_start`` below).
        #
        # Skipped only when zeroconf failed to bind — device
        # discovery already fails soft here, the advertise follows
        # the same rule.
        zeroconf = self.devices.zeroconf
        if zeroconf is None:
            _LOGGER.debug("Skipping dashboard mDNS advertise: zeroconf is unavailable")
        else:
            # ``dashboard_id`` makes the SRV target collision-free
            # ({short_hostname}-{short_dashboard_id}.local) so two
            # machines named ``mac`` on the same LAN advertise
            # distinct targets, and the system's FQDN
            # (``mac.koston.org``) can't leak through.
            dashboard_identity = await get_or_create_dashboard_identity(
                self.settings.config_dir,
                self.peer_link_identity_store,
            )
            self._dashboard_advertiser = DashboardAdvertiser(
                port=self.settings.port,
                server_version=server_version,
                esphome_version=esphome_version,
                dashboard_id=dashboard_identity.dashboard_id,
            )

        await self.remote_build_receiver.start()

        # Bind the peer-link site BEFORE advertiser register so pin
        # and port land in the initial ServiceInfo; a post-register
        # ``async_update_service`` would race python-zeroconf's
        # initial announce and flap the wire-visible TXT keys.
        await self._remote_build_lifecycle.maybe_start()

        if self._dashboard_advertiser is not None and zeroconf is not None:
            await self._dashboard_advertiser.register(zeroconf)

        # Remote-build peer browse (issue #106): browse the same
        # service type to surface peer dashboards.
        # ``OffloaderController.start`` is itself a no-op on the
        # mDNS path when zeroconf is unavailable — same fail-soft
        # contract as the advertise — so we don't gate it here.
        # Started AFTER the advertiser so the browser can capture
        # our own service-instance name and filter our broadcast
        # out of the discovered list.
        await self.remote_build_offloader.start()

        # Collect command handlers from all controllers
        for controller in (
            self.auth,
            self.boards,
            self.components,
            self.config,
            self.devices,
            self.automations,
            self.firmware,
            self.editor,
            self.labels,
            self.onboarding,
            self.remote_build_offloader,
            self.remote_build_receiver,
            self.version_history,
        ):
            self.command_handlers.update(collect_api_commands(controller))

        # Register built-in commands
        self.command_handlers["ping"] = self._cmd_ping
        self.command_handlers["subscribe_events"] = self._cmd_subscribe_events
        # `auth` is an alias for `auth/login` so both forms work on the wire.
        if "auth/login" in self.command_handlers:
            self.command_handlers["auth"] = self.command_handlers["auth/login"]

        # Start background polling
        self._bg_task = create_eager_task(self._run_background())

        _LOGGER.info(
            "Device Builder ready — config dir: %s, %d commands registered",
            self.settings.config_dir,
            len(self.command_handlers),
        )

        if self._startup_timer is not None:
            self._startup_timer.mark("controllers")
            _LOGGER.info("Startup phases: %s", self._startup_timer.summary())

    async def stop(self) -> None:
        """Shut down the application: free network sockets first, then flush local state."""
        _LOGGER.info("Shutting down ESPHome Device Builder")
        # finally so a wedged network teardown can't skip the local-state flush.
        try:
            await self._stop_network()
        finally:
            await self._stop_local()

    async def _on_shutdown(self, app: web.Application) -> None:
        """Free the network sockets early (aiohttp ``on_shutdown``)."""
        try:
            await self._stop_network()
        except Exception:
            # A raise here would abort aiohttp cleanup before on_cleanup runs.
            _LOGGER.exception("Early network teardown failed; continuing shutdown")

    async def _stop_network(self) -> None:
        """Tear down network-facing resources (remote-build, mDNS) once a full pass completes."""
        if self._network_stopped:
            return
        if self._bg_task:
            await drain_tasks((self._bg_task,), log_exceptions=True)
        await drain_tasks(self._background_tasks)
        # Tear down the remote-build listener (if it was bound)
        # before the controller it depends on. Order matters less
        # here than for zeroconf, but doing it first keeps the
        # listener from servicing a request that hits a torn-down
        # controller mid-shutdown. ``shutdown`` takes the lifecycle
        # lock so a concurrent toggle / rotate can't land a fresh
        # runner after this teardown and leak a listener past
        # shutdown.
        await self._remote_build_lifecycle.shutdown()
        # Cancel the remote-build browser BEFORE devices.stop()
        # closes the zeroconf socket the browser is using. Same
        # ordering rule as the dashboard advertise just below.
        if self.remote_build_offloader is not None:
            await self.remote_build_offloader.stop()
        if self.remote_build_receiver is not None:
            await self.remote_build_receiver.stop()
        # Withdraw the mDNS advertise BEFORE devices.stop() closes
        # the zeroconf socket the responder is using.
        if self._dashboard_advertiser is not None:
            await self._dashboard_advertiser.unregister()
            self._dashboard_advertiser = None
        if self.devices is not None:
            await self.devices.stop()
        # Latch only after a full pass, so a partial teardown (a step raising in
        # the swallowing on_shutdown hook) can still be retried by stop().
        self._network_stopped = True

    async def _stop_local(self) -> None:
        """Flush local state (editor, version history, settings) and drain the executor pool."""
        if self.firmware is not None:
            self.firmware.stop()  # sync — no await; see FirmwareController.stop()
        if self.editor is not None:
            await self.editor.stop()
        if self.version_history is not None:
            await self.version_history.stop()
        if self.config is not None:
            await self.config.stop()
        # Cleanly drain the pool once nothing else can hand it work.
        # Two paths because the pool is created eagerly in ``__init__``
        # — calling ``stop()`` on an instance that never ran
        # ``start()`` (and so never bound a loop) still has a live
        # pool to clean up.
        if self._executor is not None:
            executor = self._executor
            self._executor = None
            if self.loop is not None:
                # ``loop.shutdown_default_executor`` is the asyncio
                # idiom: it's specifically engineered to NOT route
                # through the executor being shut down (which would
                # deadlock — ``asyncio.to_thread`` would try to
                # schedule ``shutdown(wait=True)`` on the same pool
                # we're closing), waits for in-flight work, and
                # joins the worker threads. Defensively re-pin our
                # pool as the loop's default first so a third party
                # that swapped the default after ``start()`` can't
                # redirect this shutdown.
                self.loop.set_default_executor(executor)
                await self.loop.shutdown_default_executor()
            else:
                # No loop ever bound this pool — nothing has been
                # scheduled on it, so a non-blocking shutdown is
                # safe and avoids the "what loop runs to_thread"
                # question entirely.
                executor.shutdown(wait=False)

    async def _run_background(self) -> None:
        """Background polling loop.

        Drives ``DevicesController.poll`` for filesystem drift the
        push paths can't see (YAML file dropped in via SSH /
        Samba, atomic-save mid-edit, sidecar mtime change). Gated
        on ``SubscriberPresence`` — when no WS client is
        subscribed, no UI is showing the device list, so paying
        for a directory enumeration + per-file stat every 5 s is
        idle CPU we can skip. The 0→1 subscriber transition
        wakes ``wait_for_subscriber`` immediately, so the first
        client to connect picks up freshly-dropped YAMLs within
        one ``_BACKGROUND_POLL_INTERVAL_SECONDS`` instead of
        having to wait for the next scheduled tick — same shape
        ``_ping_loop`` uses for the ICMP sweep.
        """
        presence = self.subscriber_presence
        while True:
            await presence.wait_for_subscriber()
            if self.devices:
                await self.devices.poll()
            # Interruptible idle wait: bail early if the last
            # subscriber leaves so the next one to connect doesn't
            # sit through the rest of a stale interval. The
            # ``TimeoutError`` branch is the steady-state "still
            # subscribed, poll again" path; either way we loop
            # back to ``wait_for_subscriber`` which parks if the
            # gate has since closed.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    presence.wait_for_no_subscribers(),
                    timeout=_BACKGROUND_POLL_INTERVAL_SECONDS,
                )

    @staticmethod
    async def _cmd_ping(**kwargs: Any) -> dict:
        """Respond to ping."""
        return {"pong": True}

    async def _cmd_subscribe_events(
        self, *, client: Any = None, message_id: str = "", **kwargs: Any
    ) -> None:
        """
        Subscribe a connected WS client to real-time events.

        The client receives an initial device list, then ongoing events
        as devices change. Subscription is active for the connection
        lifetime; ``stream_events`` parks in its drain loop until the
        WS closes (cancelling this task), at which point the
        ``EventBus.listening`` context manager inside the helper
        runs its ``finally`` and unsubscribes every listener.

        Previous shapes had two problems addressed here:

        1. The very first version registered listeners then returned,
           leaking ~one listener per ``EventType`` per disconnected
           client. Each leaked listener kept the closed-client
           closure alive, so ``bus.fire`` iterated dead listeners
           forever and bloated the logs with stale-send errors.
        2. The interim shape forwarded events via independent
           ``asyncio.create_task`` calls, so an event fired during
           the ``initial_state`` await raced ahead and arrived
           *before* the snapshot — clients couldn't rely on
           "initial state first, then live updates" ordering.

        ``stream_events`` closes both: listeners attach inside its
        ``with bus.listening`` block before the snapshot is awaited,
        and the bounded queue serialises every event after the seed.

        Backpressure: a queue overflow forces the WS to close
        (``push_or_terminate`` for every event type). A client
        that's fallen 4000+ events behind is already in a broken
        state — its UI is showing wildly stale data — so the
        cleanest recovery is to drop the connection and let the
        client reconnect. ``initial_state`` reseeds device state
        on the new connection; for authoritative job state
        clients use ``follow_jobs`` (which has its own snapshot).
        Selectively keeping log lines or lifecycle events through
        an overflow doesn't actually leave the UI in a usable
        state — the connection is fucked either way.
        """
        if client is None:
            return

        async def _send_initial(_controls: StreamControls) -> None:
            # Snapshot every per-feature collection that the
            # frontend needs to render its initial paint without a
            # follow-up read. Importable devices and pairings are
            # populated server-side by background activity (mDNS
            # browser, ``request_pair`` outcomes), and per-event
            # diffs fire only on transitions; without seeding the
            # snapshot here a fresh page load would miss everything
            # the dashboard had already accumulated by then.
            initial: dict[str, Any] = {}
            # Gate first-paint UI, so ship them here instead of a separate
            # get_preferences round-trip. Sync RAM read off the store. Always
            # present per the wire contract; the config controller is created in
            # start() before any subscribe is served, so raise (don't silently
            # omit) if that invariant is ever broken.
            if self.config is None:  # pragma: no cover — config is always up post-start
                raise RuntimeError("config controller is not initialized")
            initial["preferences"] = self.config.prefs.snapshot().to_dict()
            if self.devices:
                initial["devices"] = [d.to_dict() for d in self.devices.get_devices()]
                initial["importable"] = [d.to_dict() for d in self.devices.get_importable_devices()]
            if self.remote_build_offloader is not None:
                # Offloader-side seeds: pairings, mDNS-discovered
                # hosts, pair alerts, per-peer queue status,
                # in-flight remote jobs, and the offloader-wide
                # toggle scalars (remote_builds_enabled, the
                # major-version-mismatch gate). Each is a sync
                # read from the controller's in-RAM dict; live
                # updates flow through subscribe_events.
                initial["pairings"] = [
                    summary.to_dict() for summary in self.remote_build_offloader.pairings_snapshot()
                ]
                initial["hosts"] = [
                    peer.to_dict() for peer in self.remote_build_offloader.hosts_snapshot()
                ]
                initial["offloader_alerts"] = list(
                    self.remote_build_offloader.offloader_alerts_snapshot()
                )
                initial["peer_queue_status"] = list(
                    self.remote_build_offloader.peer_queue_status_snapshot()
                )
                initial["remote_jobs"] = [
                    dict(entry)
                    for entry in self.remote_build_offloader.offloader_remote_jobs_snapshot()
                ]
                initial |= self.remote_build_offloader.offloader_settings_snapshot()
            if self.remote_build_receiver is not None:
                # Receiver-side peers (PENDING + APPROVED) for the
                # Pairing-requests inbox + paired list. Live
                # updates flow from
                # ``REMOTE_BUILD_PAIR_REQUEST_RECEIVED`` and
                # ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` events.
                initial["peers"] = [
                    summary.to_dict() for summary in self.remote_build_receiver.peers_snapshot()
                ]
            await client.send_event(message_id, "initial_state", initial)
            # Confirm subscription so the frontend can mark the WS
            # as live before the first event arrives.
            await client.send_result(message_id, {"subscribed": True})

        def _handle_event(event: Event, controls: StreamControls) -> None:
            data = event.data
            serialized: dict[str, Any] = {}
            for key, value in data.items():
                serialized[key] = value.to_dict() if hasattr(value, "to_dict") else value
            # Fail-closed for every event type. If the queue
            # overflows, the client is 4000+ events behind and the
            # connection is already broken; a forced disconnect +
            # reconnect (which reseeds device state from
            # ``initial_state``) is cleaner than leaving the WS
            # open with selectively-delivered events behind a
            # massive backlog.
            controls.push_or_terminate(event.event_type.value, serialized)

        # Hold a presence reference for the lifetime of the stream so
        # idle-time ICMP discovery resumes the moment a client
        # subscribes and pauses again on disconnect. The 0→1
        # transition wakes any awaiter on
        # ``presence.wait_for_subscriber``; the 1→0 transition
        # re-arms the gate so the next idle period takes effect.
        with self.subscriber_presence.subscriber():
            await stream_events(
                client=client,
                message_id=message_id,
                bus=self.bus,
                event_types=_BROADCAST_EVENT_TYPES,
                handle_event=_handle_event,
                send_initial=_send_initial,
            )

    def create_background_task(self, coro: Any) -> asyncio.Task:
        """Create a tracked background task."""
        assert self.loop is not None  # type narrowing
        task = create_eager_task(coro, loop=self.loop)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Web application
    # ------------------------------------------------------------------

    def create_app(
        self,
        *,
        trusted: bool = False,
        peer_guard: bool | None = None,
        with_lifecycle: bool = True,
        with_ingress_site: bool = True,
    ) -> web.Application:
        """
        Build the aiohttp application.

        ``trusted`` skips the auth middleware (HA Ingress site).
        ``peer_guard`` restricts sources to loopback + the supervisor;
        it defaults to ``trusted`` so the ingress site is locked down,
        but the front-door-open public site passes ``False`` to stay
        reachable from the LAN while still skipping auth.
        ``with_lifecycle`` toggles startup/cleanup hooks; the ingress
        app reuses the public app's controller singleton and so passes
        ``False`` to avoid re-initialising them.
        ``with_ingress_site`` controls whether the lifecycle hooks
        spawn the *separate* trusted ingress site alongside the
        public site. Defaults to ``True`` for the canonical
        public+ingress deployment. Pass ``False`` from the
        ingress-only fail-secure path in ``run`` (where this app
        IS the ingress) to avoid recursively spawning a second
        ingress site via ``_start_ingress_site``.
        """
        if peer_guard is None:
            peer_guard = trusted
        # The trusted ingress site bypasses auth, so the peer guard (loopback +
        # supervisor only) runs outermost to reject everything else before any
        # other processing. The public site gates by Authorization instead.
        middlewares: list[Any] = []
        if peer_guard:
            middlewares.append(ingress_peer_guard)
        middlewares.append(cors_middleware)
        if not trusted:
            middlewares.append(auth_middleware)

        app = web.Application(middlewares=middlewares)
        app["device_builder"] = self
        app["trusted_site"] = trusted
        # Seed the active-WS registry + the on_shutdown closer in
        # one place. ``close_active_websockets`` fires at app
        # shutdown so an idle paired client doesn't pin the run
        # loop to aiohttp's ``shutdown_timeout`` (60s default)
        # waiting for the ``async for msg in ws`` handler to
        # unwind; without it, SIGTERM-to-exit was 20-60s with one
        # connected client. Registered unconditionally (no
        # ``with_lifecycle`` gate) because the ingress and remote-
        # build apps share the same WS-handler shape and the same
        # latency cost on shutdown.
        init_ws_app(app)

        # WebSocket API
        app.router.add_routes(create_ws_routes())

        # Legacy REST endpoints (HA backward compat)
        app.router.add_routes(create_legacy_routes())

        # HTTP firmware-artifact download. Registered before the SPA catch-all
        # so it isn't swallowed; gated by auth_middleware (or the supervisor on
        # the ingress site). HTTP, not WS, so a large firmware.elf isn't capped
        # by a proxy's WebSocket max_msg_size.
        app.router.add_get("/api/firmware/download", firmware_http_download)

        # Health/version endpoint. Public (see auth._PUBLIC_PATHS) and
        # registered before the SPA catch-all so the upstream Docker image's
        # HEALTHCHECK gets a deterministic JSON 200 instead of the SPA shell.
        app.router.add_get("/version", _handle_version)

        # Static file serving for board images
        boards_dir = Path(__file__).parent / "definitions" / "boards"
        if boards_dir.is_dir():
            app.router.add_static("/boards/images", boards_dir)

        # Frontend serving
        frontend_dir = self._get_frontend_dir()
        if frontend_dir and frontend_dir.is_dir():
            self._register_frontend(app, frontend_dir, dev_mode=self.settings.dev_mode)
        elif with_lifecycle:
            # The ingress app is silent here — the public app already logged.
            _LOGGER.info(
                "Frontend package not installed — running in API-only mode. "
                "Install esphome-device-builder-frontend for the web UI."
            )

        if with_lifecycle:
            app.on_startup.append(self._on_startup)
            # After init_ws_app's close_active_websockets so WS handlers unwind
            # before the network teardown.
            app.on_shutdown.append(self._on_shutdown)
            # Every add-on shape needs the trusted ingress site for the HA
            # sidebar, including the front-door-open one whose main app is the
            # unauthenticated public site. The ingress-only path passes
            # ``with_ingress_site=False`` (it IS the ingress).
            if with_ingress_site and self.settings.on_ha_addon:
                app.on_startup.append(self._start_ingress_site)
                app.on_cleanup.append(self._stop_ingress_site)
            app.on_cleanup.append(self._on_cleanup)

        return app

    async def _on_startup(self, app: web.Application) -> None:
        await self.start()

    async def _on_cleanup(self, app: web.Application) -> None:
        await self.stop()

    async def _start_ingress_site(self, _: web.Application) -> None:
        """Start the trusted HA Ingress TCP site alongside the public site."""
        hosts = self.settings.ingress_bind_hosts
        ensure_single_host_for_ephemeral_port(hosts, self.settings.ingress_port, "--ingress-port")
        ingress_app = self.create_app(trusted=True, with_lifecycle=False)
        runner = web.AppRunner(ingress_app)
        await runner.setup()
        # Partial-bind cleanup: a multi-host expansion can succeed
        # on host[0] and fail on host[1]; without this guard the
        # runner (still owning the host[0] socket) would go out of
        # scope before ``self._ingress_runner`` is assigned, so
        # ``_stop_ingress_site`` would see ``None`` and leak the
        # bound port until process exit.
        try:
            for host in hosts:
                site = web.TCPSite(runner, host, self.settings.ingress_port)
                await site.start()
                _LOGGER.info(
                    "Ingress site listening on %s:%d (trusted, bypasses auth)",
                    host,
                    self.settings.ingress_port,
                )
        except Exception:
            with contextlib.suppress(Exception):
                await runner.cleanup()
            raise
        self._ingress_runner = runner

    async def _stop_ingress_site(self, _: web.Application) -> None:
        if self._ingress_runner is not None:
            await self._ingress_runner.cleanup()
            self._ingress_runner = None

    def _warn_front_door_open(self) -> None:
        """Log the wide-open banner before binding the unauthenticated public port."""
        settings = self.settings
        banner = "=" * 70
        _LOGGER.warning(
            "\n%s\n"
            " FRONT DOOR OPEN: external authentication is DISABLED.\n"
            " The dashboard is serving on %s:%d with NO authentication.\n"
            " ANYONE on your network can flash firmware and run code on this host.\n"
            ' You enabled this with the add-on option "Disable external\n'
            ' authentication" (leave_front_door_open) plus a mapped port %d.\n'
            " There is no password, no login, no protection of any kind.\n"
            " Turn that option OFF (or unmap the port) for ingress-only access.\n"
            "%s",
            banner,
            settings.host,
            settings.port,
            settings.port,
            banner,
        )

    def run(self) -> None:
        """Start the HTTP server (blocking)."""
        # Logging is already configured by __main__.py
        settings = self.settings
        # On the HA add-on with no password we never gate the public
        # port with HA credentials — the legacy supervisor ``/auth``
        # fallback is gone (an unrate-limited brute-force vector,
        # issue #85). The default is ingress-only: the dashboard is
        # reached through the HA sidebar and the public port stays
        # unbound. The one exception is the operator's explicit,
        # two-part opt-in to a wide-open dashboard — the
        # ``leave_front_door_open`` add-on option *and* a mapped port
        # 6052 — which binds the public port with no auth at all
        # (legacy parity; legacy required both too).
        if settings.on_ha_addon and not settings.using_password:
            if settings.serve_public_unauthenticated:
                self._warn_front_door_open()
                # peer_guard=False so LAN clients (e.g. the VS Code ESPHome
                # plugin) can reach it; the ingress site is still bound via the
                # on_ha_addon lifecycle hook so the HA sidebar keeps working.
                # trusted=False keeps the WS origin/Host gate active so a plain
                # cross-origin browser drive-by is still rejected; auth itself is
                # a no-op here because the add-on configures no password, so the
                # site stays unauthenticated for same-origin and non-browser
                # clients (which omit Origin) without paying the open-origin cost.
                app = self.create_app(trusted=False, peer_guard=False)
                if self._startup_timer is not None:
                    self._startup_timer.mark("app")
                hosts = resolve_bind_host(settings.host)
                ensure_single_host_for_ephemeral_port(hosts, settings.port, "--port")
                web.run_app(
                    app,
                    host=hosts,
                    port=settings.port,
                    shutdown_timeout=_SHUTDOWN_TIMEOUT_SECONDS,
                    handle_signals=False,
                )
                return
            if settings.front_door_open:
                # Front door open but the port isn't mapped, so there's
                # nothing to expose (legacy parity: nginx only listened
                # on 6052 when the operator mapped it).
                _LOGGER.warning(
                    'Public port %d NOT bound: "Disable external authentication" is '
                    "on but port 6052 is not mapped, so nothing is exposed on the "
                    "LAN. Map the port in the add-on Network options to expose the "
                    "dashboard without auth.",
                    settings.port,
                )
            elif settings.allow_public_port:
                # Mapped port without front-door-open: the new dashboard
                # has no HA-credential gate to put on it (#85), so it
                # stays ingress-only rather than silently exposing it.
                _LOGGER.warning(
                    "Public port %d NOT bound: port 6052 is mapped but the new "
                    "dashboard can't gate it with Home Assistant credentials (see "
                    'issue #85). Turn on "Disable external authentication" to expose '
                    "it without auth, or run the standalone PyPI install for "
                    "password-gated LAN access.",
                    settings.port,
                )
            else:
                _LOGGER.warning(
                    "Public port %d NOT bound: the HA add-on is "
                    "ingress-only by design and doesn't expose "
                    "USERNAME/PASSWORD options. The dashboard is "
                    "reachable through the Home Assistant UI. For "
                    "password-gated LAN access, run the standalone PyPI "
                    'install on the same network. See README "Home '
                    'Assistant add-on".',
                    settings.port,
                )
            app = self.create_app(trusted=True, with_ingress_site=False)
            if self._startup_timer is not None:
                self._startup_timer.mark("app")
            hosts = settings.ingress_bind_hosts
            ensure_single_host_for_ephemeral_port(hosts, settings.ingress_port, "--ingress-port")
            web.run_app(
                app,
                host=hosts,
                port=settings.ingress_port,
                shutdown_timeout=_SHUTDOWN_TIMEOUT_SECONDS,
                handle_signals=False,
            )
            return
        app = self.create_app()
        if self._startup_timer is not None:
            self._startup_timer.mark("app")
        hosts = resolve_bind_host(settings.host)
        ensure_single_host_for_ephemeral_port(hosts, settings.port, "--port")
        # ``handle_signals=False``: keep our ``__main__`` SIGTERM/SIGBREAK trap
        # as the sole handler for the whole lifecycle. aiohttp's own
        # ``add_signal_handler`` is armed inside ``runner.setup()`` *before*
        # ``on_startup`` runs and would otherwise replace our trap, so a stop
        # landing mid-startup would take aiohttp's path and bypass the
        # clean-exit bookkeeping in ``main``. Our trap defers ``GracefulExit``
        # to the loop just the same, so ``run_app`` still drains ``on_cleanup``
        # while serving.
        web.run_app(
            app,
            host=hosts,
            port=settings.port,
            shutdown_timeout=_SHUTDOWN_TIMEOUT_SECONDS,
            handle_signals=False,
        )

    @staticmethod
    def _get_frontend_dir() -> Path | None:
        """Return the path to the built frontend, or None if unavailable."""
        # The companion wheel ``esphome-device-builder-frontend``
        # normally ships the prebuilt assets for dependency-managed
        # installs, but keep the import lazy (the PLC0415
        # suppression below) so this method still handles runtime
        # environments where it is unavailable and can be patched in
        # tests via ``builtins.__import__`` without re-importing the
        # module — see test_ha_addon_failsafe's ImportError coverage.
        try:
            from esphome_device_builder_frontend import where  # noqa: PLC0415

            return Path(where())
        except ImportError:
            return None

    @staticmethod
    def _register_frontend(  # noqa: C901
        app: web.Application, frontend_dir: Path, *, dev_mode: bool = False
    ) -> None:
        """Register routes for the built frontend.

        Refuses to start if the installed wheel is missing
        ``index.html`` or the ``assets/`` tree.

        ``add_static("/assets")`` serves images via aiohttp's vetted
        static handler (sendfile + traversal protection). Top-level
        bundles and the SPA fallback share a single catch-all GET
        registered last, so aiohttp's FIFO route lookup matches every
        explicit server route first; only paths nothing else claimed
        reach this handler. Multi-segment paths never touch the
        filesystem here, which keeps traversal impossible by
        construction.

        ``dev_mode`` flips the SPA shell to ``Cache-Control: no-cache``
        so a re-deployed wheel isn't masked by a browser-cached
        ``index.html`` that points at a now-deleted hashed bundle.
        Hashed bundles are served as ``immutable`` regardless — their
        filenames are content-addressed by definition.
        """
        index_html = frontend_dir / "index.html"
        assets_dir = frontend_dir / "assets"
        missing: list[str] = []
        if not index_html.is_file():
            missing.append("index.html")
        if not assets_dir.is_dir():
            missing.append("assets/")
        if missing:
            raise RuntimeError(
                f"Frontend at {frontend_dir} is missing required entries: "
                f"{', '.join(missing)}. The installed "
                "esphome-device-builder-frontend wheel looks broken — "
                "rebuild it (`npm run build` in the frontend repo) and "
                "reinstall, or uninstall it to run in API-only mode."
            )

        frontend_root = frontend_dir.resolve()
        shell_headers = _NO_CACHE_HEADERS if dev_mode else None
        index_html_text = index_html.read_text(encoding="utf-8")
        if _BASE_HREF_PLACEHOLDER not in index_html_text:
            raise RuntimeError(
                f"Frontend index.html at {index_html} is missing the "
                f"{_BASE_HREF_PLACEHOLDER!r} placeholder — the wheel is "
                "out of sync with the backend's expected template."
            )

        @lru_cache(maxsize=8)
        def _shell_html(base_href: str) -> str:
            """Cache rendered ``index.html`` per deployment base.

            Substituting a single placeholder is cheap, but doing it
            on every request adds up under load. Cap at 8 entries —
            most deployments hit one or two distinct prefixes (root
            + maybe ingress).
            """
            return index_html_text.replace(
                _BASE_HREF_PLACEHOLDER, html.escape(base_href, quote=True)
            )

        def _render_shell(request: web.Request, *, tail: str = "") -> web.Response:
            response = web.Response(
                text=_shell_html(_resolve_base_href(request, tail=tail)),
                content_type="text/html",
                headers=shell_headers,
            )
            # The rendered shell varies by ``X-Forwarded-Prefix`` —
            # without ``Vary`` an intermediary cache could serve a
            # response built for one prefix to a request behind a
            # different proxy.
            response.headers["Vary"] = _BASE_HREF_VARY
            return response

        async def handle_index(request: web.Request) -> web.Response:
            return _render_shell(request)

        def _resolve_static(candidate: Path) -> Path | None:
            """Return the candidate if it's a real file inside ``frontend_root``.

            Combined into one helper so the per-request stat / resolve
            chain runs in a single thread hop instead of three. Refuses
            to follow symlinks pointing outside the frontend directory
            — matches ``add_static``'s default safety.
            """
            try:
                if candidate.is_file() and candidate.resolve().is_relative_to(frontend_root):
                    return candidate
            except OSError:
                return None
            return None

        async def handle_spa(request: web.Request) -> web.StreamResponse:
            tail = request.match_info["tail"]
            # Only flat names (hashed bundles, license sidecars) get
            # served from disk. Anything with a path separator is an
            # SPA deep link that the client router will resolve.
            if tail and "/" not in tail:
                candidate = frontend_dir / tail
                resolved = await asyncio.to_thread(_resolve_static, candidate)
                if resolved is not None:
                    headers = (
                        _IMMUTABLE_HEADERS if HASHED_FILENAME_RE.search(tail) else shell_headers
                    )
                    return web.FileResponse(resolved, headers=headers)
            # 404 asset-shaped requests instead of returning the SPA
            # shell so the browser doesn't try to parse HTML as JS /
            # CSS / etc. on a hard-reload of a deep URL — see
            # ``_ASSET_EXTENSIONS`` for the rationale.
            if tail and Path(tail).suffix.lower() in _ASSET_EXTENSIONS:
                raise web.HTTPNotFound
            return _render_shell(request, tail=tail)

        app.router.add_static("/assets", assets_dir)
        app.router.add_get("/", handle_index)
        app.router.add_get("/{tail:.*}", handle_spa)

        _LOGGER.info("Serving frontend from %s (dev_mode=%s)", frontend_dir, dev_mode)
