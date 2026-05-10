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
import re
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .helpers.peer_link_identity import PeerLinkIdentity

from aiohttp import web
from esphome.const import __version__ as esphome_version

from .api.legacy import create_legacy_routes
from .api.ws import create_ws_routes
from .constants import __version__ as server_version
from .controllers.auth import AuthController
from .controllers.automations import AutomationsController
from .controllers.boards import BoardCatalog
from .controllers.components import ComponentCatalog
from .controllers.config import (
    ConfigController,
    DashboardSettings,
    load_remote_build_settings,
)
from .controllers.devices import DevicesController
from .controllers.editor import EditorController
from .controllers.firmware import FirmwareController
from .controllers.labels import LabelsController
from .controllers.onboarding import OnboardingController
from .controllers.remote_build import RemoteBuildController
from .controllers.remote_build_peer_link import PEER_LINK_PATH, make_peer_link_handler
from .helpers.api import CommandHandler, collect_api_commands
from .helpers.auth import auth_middleware
from .helpers.dashboard_advertise import DashboardAdvertiser
from .helpers.event_bus import Event, EventBus, StreamControls, stream_events
from .helpers.json import cors_middleware
from .helpers.peer_link_identity import get_or_create_peer_link_identity
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

# Cache policy for the SPA shell:
#   - ``index.html`` and any non-hashed top-level file: must always
#     revalidate so a re-deployed wheel doesn't get masked by a
#     stale browser cache.
#   - Hashed bundles (``app.<hash>.js``, ``vendors.<hash>.js``,
#     license sidecars) are content-addressed — the filename changes
#     on every rebuild, so they're safe to cache forever.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}
_IMMUTABLE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}
_HASHED_FILENAME_RE = re.compile(r"\.[a-f0-9]{8,}\.")

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


@web.middleware
async def _strip_server_header_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """
    Override aiohttp's default ``Server: Python/x.y aiohttp/z.w`` banner.

    Defence-in-depth on the peer-link receiver surface: the banner
    is a free version-fingerprint for any scanner that touches the
    listener. Stripping the header costs nothing and keeps the
    signal off the wire.

    aiohttp injects the banner at the connection-write layer
    when the response doesn't carry a ``Server`` header — a
    middleware-level ``del`` only catches handlers that set the
    header explicitly. Setting the header to an empty string
    overrides aiohttp's default; an empty ``Server:`` value
    lands on the wire instead of the version banner.
    """
    response = await handler(request)
    response.headers["Server"] = ""
    return response


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


# Worker-thread budget for the default ``ThreadPoolExecutor``. asyncio's
# default is ``min(32, os.cpu_count() + 4)`` — too tight for the
# dashboard's I/O-bound workload (DNS resolves on every ping sweep,
# scanner stats, YAML parses, MQTT TCP connect) once the device count
# crosses ~30. 64 leaves comfortable headroom on a saturated sweep
# without fanning out so wide that the OS thread table balloons. Keep
# this as a module-level constant so the value is one place to audit
# and the test suite's pin-down assertion can reference it.
_EXECUTOR_MAX_WORKERS = 64


class DeviceBuilder:
    """Core application singleton.

    Owns controllers, event bus, command registry, and web app.
    All device state lives in DevicesController.
    """

    def __init__(self, settings: DashboardSettings) -> None:
        """Initialize the Device Builder."""
        self.settings = settings
        self.bus = EventBus()
        # Reference-counted "is anyone watching the dashboard?" gate.
        # The ``subscribe_events`` body wraps itself in
        # ``presence.subscriber()`` so consumers — currently the
        # state monitor's ICMP ping loop — can park while the gate
        # is closed and resume on the 0→1 transition. Mirrors the
        # legacy dashboard's ``ping_request`` / ``self._subscribers``
        # pair so a quiet network with no observers generates no
        # ICMP traffic.
        self.subscriber_presence = SubscriberPresence()
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
        self.remote_build: RemoteBuildController | None = None

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

        self._ingress_runner: web.AppRunner | None = None
        # Peer-link Noise WS receiver site for
        # ``/remote-build/peer-link`` (issue #106 phase 4a-r1).
        # Bound only when ``RemoteBuildSettings.enabled`` is true;
        # ``None`` otherwise.
        self._remote_build_runner: web.AppRunner | None = None
        # Serialises listener-state mutations so two clients
        # toggling ``set_settings`` (or a ``rotate_identity``
        # racing a toggle) can't interleave their teardown +
        # rebind sequences. Lazy-init at first acquire so the
        # lock binds to the running event loop, not the loop
        # that ran ``__init__``.
        self._remote_build_lifecycle_lock: asyncio.Lock | None = None

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
        self.remote_build = RemoteBuildController(self)
        await self.devices.start()
        await self.firmware.start()
        await self.editor.start()

        # Advertise this dashboard on mDNS so peer dashboards (and
        # the future ESPHome Desktop welcome screen) can discover it.
        # Reuses the state monitor's zeroconf instance so the
        # responder count stays at one per process.
        #
        # Skipped in two cases:
        #   * Zeroconf failed to bind — device discovery already
        #     fails soft here, the advertise follows the same rule.
        #   * HA addon — by default the addon container's port 6052
        #     is not exposed to the LAN (ingress-only on 8099) AND
        #     mDNS announcements would carry the container's docker
        #     IP rather than the host's, so a peer that found the
        #     listing couldn't connect anyway. A future setting can
        #     opt back in once we know how to expose the addon's
        #     host port deliberately.
        zeroconf = self.devices.zeroconf
        if zeroconf is None:
            _LOGGER.debug("Skipping dashboard mDNS advertise: zeroconf is unavailable")
        elif self.settings.on_ha_addon:
            _LOGGER.debug(
                "Skipping dashboard mDNS advertise: running as HA addon "
                "(ingress-only; port 6052 not LAN-reachable)"
            )
        else:
            self._dashboard_advertiser = DashboardAdvertiser(
                port=self.settings.port,
                server_version=server_version,
                esphome_version=esphome_version,
            )
            await self._dashboard_advertiser.register(zeroconf)

        # Phase 2 of the remote-build feature (issue #106): browse
        # the same service type to surface peer dashboards.
        # ``RemoteBuildController.start`` is itself a no-op when
        # zeroconf is unavailable — same fail-soft contract as the
        # advertise — so we don't gate it here. Started AFTER the
        # advertiser so the browser can capture our own
        # service-instance name and filter our broadcast out of the
        # discovered list.
        await self.remote_build.start()

        # Bind the peer-link Noise WS receiver site at
        # ``/remote-build/peer-link`` if the user has opted in via
        # ``RemoteBuildSettings.enabled``. Default-off so a
        # fresh install never opens an inbound port without a
        # deliberate operator action.
        await self._maybe_start_remote_build_site()

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
            self.remote_build,
        ):
            self.command_handlers.update(collect_api_commands(controller))

        # Register built-in commands
        self.command_handlers["ping"] = self._cmd_ping
        self.command_handlers["subscribe_events"] = self._cmd_subscribe_events
        # `auth` is an alias for `auth/login` so both forms work on the wire.
        if "auth/login" in self.command_handlers:
            self.command_handlers["auth"] = self.command_handlers["auth/login"]

        # Start background polling
        self._bg_task = asyncio.create_task(self._run_background())

        _LOGGER.info(
            "Device Builder ready — config dir: %s, %d commands registered",
            self.settings.config_dir,
            len(self.command_handlers),
        )

    async def stop(self) -> None:
        """Shut down the application."""
        if self._bg_task:
            self._bg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bg_task
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        # Tear down the remote-build HTTPS listener (if it was
        # bound) before the controller it depends on. Order
        # matters less here than for zeroconf, but doing it
        # first keeps the listener from servicing a request
        # that hits a torn-down controller mid-shutdown.
        # Acquire ``_remote_build_lifecycle_lock`` so a
        # concurrent ``apply_remote_build_enabled`` /
        # ``reload_remote_build_identity`` can't interleave its
        # rebind with this teardown — without the lock, an
        # in-flight toggle could land a fresh runner *after*
        # ``stop()`` has cleared the slot, leaking a listener
        # past shutdown.
        async with self._get_remote_build_lifecycle_lock():
            if self._remote_build_runner is not None:
                with contextlib.suppress(Exception):
                    await self._remote_build_runner.cleanup()
                self._remote_build_runner = None
        # Cancel the remote-build browser BEFORE devices.stop()
        # closes the zeroconf socket the browser is using. Same
        # ordering rule as the dashboard advertise just below.
        if self.remote_build is not None:
            await self.remote_build.stop()
        # Withdraw the mDNS advertise BEFORE devices.stop() closes
        # the zeroconf socket the responder is using.
        if self._dashboard_advertiser is not None:
            await self._dashboard_advertiser.unregister()
            self._dashboard_advertiser = None
        if self.devices is not None:
            await self.devices.stop()
        if self.editor is not None:
            await self.editor.stop()
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
            if self.devices:
                initial["devices"] = [d.to_dict() for d in self.devices.get_devices()]
                initial["importable"] = [d.to_dict() for d in self.devices.get_importable_devices()]
            if self.remote_build is not None:
                # Pairings (PENDING + APPROVED) so the frontend's
                # Send-builds initial paint matches what
                # ``OFFLOADER_PAIR_STATUS_CHANGED`` events will
                # mutate against. Sync read from the controller's
                # in-RAM ``_pairings`` dict — no wire calls, no
                # disk I/O.
                initial["pairings"] = [
                    summary.to_dict() for summary in self.remote_build.pairings_snapshot()
                ]
                # Receiver-side peers (PENDING + APPROVED) for the
                # Pairing-requests inbox + paired list. Same RAM
                # snapshot pattern as ``pairings``: live updates
                # flow from ``REMOTE_BUILD_PAIR_REQUEST_RECEIVED``
                # and ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` events
                # through the same ``subscribe_events`` stream.
                initial["peers"] = [
                    summary.to_dict() for summary in self.remote_build.peers_snapshot()
                ]
                # mDNS-discovered peer dashboards (RAM-only,
                # never persisted). Live updates flow from
                # ``REMOTE_BUILD_HOST_ADDED`` /
                # ``REMOTE_BUILD_HOST_REMOVED`` events fired by
                # the receiver controller's mDNS browser
                # callbacks.
                initial["hosts"] = [peer.to_dict() for peer in self.remote_build.hosts_snapshot()]
                # Offloader-side pair alerts (RAM-only on the
                # controller; populated when pair-status detection
                # finds a drifted receiver pin or a REJECTED
                # response). Late-subscribers pick up alerts they
                # missed on the live stream via this snapshot.
                # ``OFFLOADER_PAIR_PIN_MISMATCH`` /
                # ``OFFLOADER_PAIR_PEER_REVOKED`` /
                # ``OFFLOADER_PAIR_ALERT_DISMISSED`` events drive
                # subsequent mutations.
                initial["offloader_alerts"] = list(self.remote_build.offloader_alerts_snapshot())
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

        # ``DEVICE_REACHABILITY`` is intentionally excluded — it fires
        # on every per-signal observation (every mDNS announce, every
        # ping success, every MQTT discover response) for *every*
        # configured device, which would push 60+ events/min/device
        # at every connected client. The drawer's per-device
        # subscription is the only consumer; broadcasting these
        # would defeat the point of having a per-device stream and
        # could trip the bounded queue's backpressure terminator
        # under fleet load.
        broadcast_event_types = [et for et in EventType if et is not EventType.DEVICE_REACHABILITY]
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
                event_types=broadcast_event_types,
                handle_event=_handle_event,
                send_initial=_send_initial,
            )

    def create_background_task(self, coro: Any) -> asyncio.Task:
        """Create a tracked background task."""
        assert self.loop is not None  # type narrowing
        task = self.loop.create_task(coro)
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
        with_lifecycle: bool = True,
        with_ingress_site: bool = True,
    ) -> web.Application:
        """
        Build the aiohttp application.

        ``trusted`` skips the auth middleware (HA Ingress site).
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
        middlewares: list[Any] = [cors_middleware]
        if not trusted:
            middlewares.append(auth_middleware)

        app = web.Application(middlewares=middlewares)
        app["device_builder"] = self
        app["trusted_site"] = trusted

        # WebSocket API
        app.router.add_routes(create_ws_routes())

        # Legacy REST endpoints (HA backward compat)
        app.router.add_routes(create_legacy_routes())

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
            if with_ingress_site and self.settings.create_ingress_site:
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
        ingress_app = self.create_app(trusted=True, with_lifecycle=False)
        runner = web.AppRunner(ingress_app)
        await runner.setup()
        host = self.settings.ingress_host or "0.0.0.0"
        site = web.TCPSite(runner, host, self.settings.ingress_port)
        await site.start()
        self._ingress_runner = runner
        _LOGGER.info(
            "Ingress site listening on %s:%d (trusted, bypasses auth)",
            host,
            self.settings.ingress_port,
        )

    async def _stop_ingress_site(self, _: web.Application) -> None:
        if self._ingress_runner is not None:
            await self._ingress_runner.cleanup()
            self._ingress_runner = None

    async def _maybe_start_remote_build_site(self) -> None:
        """
        Bind the peer-link Noise WS listener if remote-build is enabled.

        Default-off: the listener only binds when
        ``RemoteBuildSettings.enabled`` is true. Loads the X25519
        peer-link identity off disk via
        :func:`get_or_create_peer_link_identity` (separate from the
        3a Ed25519 cert; the cert is no longer involved on this
        listener). Hops through ``run_in_executor`` because the
        helper is sync-blocking by design.

        Fail-soft: any exception during identity load or bind is
        caught and logged. The main dashboard keeps running; the
        operator gets a warning and the listener is simply absent
        until the next restart with the issue resolved.

        On HA addon mode with the listener enabled, logs a warning
        that the addon's ``ports:`` config must expose the bound
        port for LAN peers to actually reach it. The bind itself
        proceeds; "not supported by default today" rather than
        "hard-refused forever."
        """
        if self.remote_build is None or self.loop is None:
            return
        loop = self.loop
        rb_settings = await loop.run_in_executor(
            None, load_remote_build_settings, self.settings.config_dir
        )
        if not rb_settings.enabled:
            _LOGGER.debug(
                "Skipping remote-build peer-link site: not enabled in settings "
                "(set ``remote_build/set_settings`` enabled=true to bind)"
            )
            return

        try:
            runner, identity, port = await self._build_and_start_remote_build_runner()
        except Exception:
            _LOGGER.exception(
                "Remote-build peer-link site failed to start; dashboard continues "
                "without the receiver listener. Disable in Settings or "
                "fix the underlying error and restart."
            )
            return
        self._remote_build_runner = runner

        # Update the mDNS advertise AFTER the bind succeeds. If the
        # bind raised (port in use, permission denied, ...) the
        # advertiser stays at its pre-listener state instead of
        # broadcasting a pin + port that nothing's actually
        # listening on.
        await self._publish_remote_build_advertise(
            pin_sha256=identity.pin_sha256,
            remote_build_port=port,
        )

        if self.settings.on_ha_addon:
            _LOGGER.warning(
                "Remote-build peer-link site bound on %s:%d while running as HA "
                "addon. Peers won't reach this port unless the addon's "
                "``ports:`` config exposes it to the host.",
                self.settings.host,
                port,
            )
        else:
            _LOGGER.info(
                "Remote-build peer-link site listening on %s:%d (peer-link pin %s)",
                self.settings.host,
                port,
                identity.pin_sha256_formatted,
            )

    async def _publish_remote_build_advertise(
        self,
        *,
        pin_sha256: str | None,
        remote_build_port: int | None,
    ) -> None:
        """
        Push pin / port updates to the mDNS advertise, fail-soft on refresh.

        Centralises the setter-then-refresh dance shared by
        ``_maybe_start_remote_build_site`` (post-bind: real pin +
        port) and ``reload_remote_build_identity`` (post-teardown:
        ``None`` + ``None`` to clear both fields out of TXT until
        the rebuild succeeds). Both fields are always updated;
        the contract is "``pin_sha256`` and ``remote_build_port``
        appear in TXT iff the listener is currently bound", so
        peers re-browsing while the listener is down see neither
        field and don't try to connect to a port that's no
        longer serving traffic. The explicit ``refresh`` call
        republishes the ServiceInfo if any TXT property changed;
        without it the setter-driven update would only land on
        the wire on the next periodic refresh tick (5 min). A
        flaky zeroconf refresh is swallowed so caller paths
        (bind, rotate) don't fail just because the responder is
        wedged.

        No-op when no advertiser is attached.
        """
        advertiser = self._dashboard_advertiser
        if advertiser is None:
            return
        advertiser.set_pin_sha256(pin_sha256)
        advertiser.set_remote_build_port(remote_build_port)
        with contextlib.suppress(Exception):
            await advertiser.refresh()

    @property
    def is_remote_build_listener_bound(self) -> bool:
        """True iff the remote-build peer-link Noise WS listener is currently bound."""
        return self._remote_build_runner is not None

    def _get_remote_build_lifecycle_lock(self) -> asyncio.Lock:
        """Lazy-init the lock against the running loop on first acquire."""
        if self._remote_build_lifecycle_lock is None:
            self._remote_build_lifecycle_lock = asyncio.Lock()
        return self._remote_build_lifecycle_lock

    async def _teardown_remote_build_runner(self) -> None:
        """
        Stop the bound peer-link listener and clear its mDNS advertise.

        Caller MUST hold :attr:`_remote_build_lifecycle_lock`. No-op
        when the listener isn't bound. Sequencing matters: the
        runner reference is cleared *before* awaiting cleanup so a
        concurrent listener-state observer sees the steady "absent"
        state from the moment we commit to teardown, and the mDNS
        clear runs *after* cleanup so peers re-browsing during the
        window get a TXT without ``pin_sha256`` / ``remote_build_port``
        the moment the port stops serving traffic.
        """
        if self._remote_build_runner is None:
            return
        old_runner = self._remote_build_runner
        self._remote_build_runner = None
        with contextlib.suppress(Exception):
            await old_runner.cleanup()
        await self._publish_remote_build_advertise(
            pin_sha256=None,
            remote_build_port=None,
        )

    async def apply_remote_build_enabled(self) -> bool:
        """
        Converge the peer-link listener to the on-disk ``enabled`` flag.

        Called by ``RemoteBuildController.set_settings`` after the
        new ``enabled`` value lands on disk. Reads back from disk
        under :attr:`_remote_build_lifecycle_lock` so the
        last-writer-wins persisted value is always what the
        listener converges to — two clients flipping ``enabled``
        concurrently can't desync disk from listener state.

        On disk ``enabled=True`` with the listener absent, runs the
        same path :meth:`_maybe_start_remote_build_site` does at
        startup (load X25519 peer-link identity, bind plain-TCP
        TCPSite, push pin + port to mDNS). Fail-soft on bind error
        — the dashboard keeps running without a listener, and a
        subsequent ``set_settings`` retry can clear a transient
        port conflict without a restart.

        On disk ``enabled=False`` with the listener bound, tears
        down the runner and clears ``pin_sha256`` + ``remote_build_port``
        from mDNS via :meth:`_teardown_remote_build_runner`.

        Returns whether the listener is bound after this call.
        """
        if self.loop is None:
            return self._remote_build_runner is not None
        loop = self.loop
        async with self._get_remote_build_lifecycle_lock():
            rb_settings = await loop.run_in_executor(
                None, load_remote_build_settings, self.settings.config_dir
            )
            if rb_settings.enabled:
                if self._remote_build_runner is None:
                    await self._maybe_start_remote_build_site()
            else:
                await self._teardown_remote_build_runner()
            return self._remote_build_runner is not None

    async def reload_remote_build_identity(self, *, pin_sha256: str) -> bool:
        """
        Rebuild the peer-link listener after a TLS cert rotation.

        Wired up to ``RemoteBuildController.rotate_identity`` (phase
        3c1) right after :func:`rotate_certificate` writes the new
        cert + key to disk. **This rotation path is dormant after
        the phase 4a-r1 pivot** — the listener no longer terminates
        TLS, the pin advertised in mDNS is now the X25519 peer-link
        identity's ``pin_sha256_formatted`` (loaded once at handler-
        factory time), and the cert + key on disk are unused by the
        receiver site. Calling this method still tears down + rebinds
        the listener (so the X25519 identity is reloaded from disk
        too), but the ``pin_sha256`` argument is the *cert* SPKI hash
        that no peer pins against any longer.

        Phase 4a-r2 (issue #106) tears out the dormant cert-rotation
        WS commands and replaces them with a peer-link identity
        rotation that actually rotates the right key + invalidates
        every paired peer; until then this method is a vestigial
        no-op for the rotation contract that just happens to also
        rebind the X25519 identity as a side effect.

        When the listener is bound, three side effects in order:

        * Listener teardown — the bound runner is still holding
          the old X25519 peer-link identity in its handler closure.
          Without a rebuild, the next session would still drive the
          handshake against the old key.
        * mDNS clear — both ``pin_sha256`` and ``remote_build_port``
          drop out of TXT immediately. The TXT contract is
          "these fields appear iff the listener is currently
          bound", so peers re-browsing during the rebuild window
          (or after a rebuild failure) don't try to connect to
          a port that's no longer serving traffic. Sequencing
          matters: clear comes BEFORE rebuild so that on rebuild
          failure the cleared state is the steady state.
        * Listener rebuild — re-runs the same path
          ``_maybe_start_remote_build_site`` does at startup, which
          loads the (post-rotation but unused) cert + the X25519
          peer-link identity from disk, and (on success) re-pushes
          the new peer-link pin + port to mDNS. Fail-soft: a rebuild
          failure leaves the dashboard running without a receiver
          listener (same contract as the initial bind), and the
          return value reflects that so the rotater can surface
          the failure to the operator.

        When the listener is NOT bound, this method is a no-op:
        no mDNS push (there's no listener for peers to connect
        to anyway, and pushing a pin without a port would
        contradict the TXT contract). The cert + key on disk are
        already updated by the time this method runs; the next
        bind picks them up.

        Returns whether the receiver listener is currently bound
        after this call. ``True`` means the rebind landed; ``False``
        means rotation landed on disk but no listener is serving
        (rebuild fail-softed, or listener wasn't bound to begin
        with).
        """
        del pin_sha256  # currently unused on this side; see docstring
        async with self._get_remote_build_lifecycle_lock():
            if self._remote_build_runner is None:
                return False
            # ``_teardown_remote_build_runner`` clears the advertise
            # too, so peers re-browsing during the rebuild window —
            # or after a rebuild failure — don't see stale pin +
            # port pointing at a listener that isn't there.
            # ``_maybe_start_remote_build_site`` re-pushes both on
            # a successful rebuild.
            await self._teardown_remote_build_runner()
            await self._maybe_start_remote_build_site()
            return self._remote_build_runner is not None

    async def _build_and_start_remote_build_runner(
        self,
    ) -> tuple[web.AppRunner, PeerLinkIdentity, int]:
        """
        Construct the runner and bind the peer-link Noise WS listener.

        Loads the X25519 peer-link identity (separate from the 3a
        Ed25519 cert; see PR #473) and binds a plain-TCP TCPSite
        serving exactly one route: the WS upgrade at
        ``/remote-build/peer-link``. Noise XX provides
        confidentiality + mutual auth + forward secrecy at the
        application layer, so there's no SSL context to manage and
        no cert hot-swap when the 3a cert rotates.

        Phases 3b1-3c shipped this same listener as HTTPS+bearer;
        phase 4a-r1 part 4 swaps the body to plain-TCP / Noise WS
        on the same port + flag + constant + runner slot. The
        bearer machinery (token CRUD, auth middleware, models) is
        now dormant and gets deleted in 4a-r2.

        Returns ``(runner, identity, bound_port)`` on success; on
        any exception, cleans up the partial runner before
        re-raising so the caller's ``except`` only has to log +
        return.

        ``bound_port`` is the OS-assigned port when the operator
        passed ``--remote-build-port 0`` (ephemeral); otherwise the
        configured value verbatim. Reading the real port off the
        socket prevents mDNS / log lines from claiming port 0.
        """
        loop = self.loop
        assert loop is not None  # caller-checked
        assert self.remote_build is not None  # caller-checked

        runner: web.AppRunner | None = None
        try:
            identity = await loop.run_in_executor(
                None, get_or_create_peer_link_identity, self.settings.config_dir
            )
            app = web.Application(middlewares=[_strip_server_header_middleware])
            handler = await make_peer_link_handler(self.remote_build, self.settings.config_dir)
            app.router.add_get(PEER_LINK_PATH, handler)

            runner = web.AppRunner(app)
            await runner.setup()
            configured_port = self.settings.remote_build_port
            # ``reuse_address=True`` is the asyncio default on POSIX
            # but defaults to False on Windows; pin it explicitly so
            # the rotation rebuild path
            # (``reload_remote_build_identity`` → teardown → re-bind)
            # doesn't TIME_WAIT-block on a fixed configured port
            # (default 6055) cross-platform. The ephemeral-port test
            # path masks this risk because the OS picks a fresh port
            # each rebuild; production deploys with a fixed port.
            site = web.TCPSite(
                runner,
                self.settings.host,
                configured_port,
                reuse_address=True,
            )
            await site.start()
        except Exception:
            if runner is not None:
                with contextlib.suppress(Exception):
                    await runner.cleanup()
            raise

        # Resolve the actually-bound port. ``configured_port=0``
        # tells the OS to pick an ephemeral port; the bound port
        # lives on the started server socket.
        port = configured_port
        if configured_port == 0 and site._server is not None:
            # typeshed's ``asyncio.AbstractServer`` doesn't expose
            # ``sockets`` even though the concrete ``base_events.Server``
            # does — the asyncio docs list it as part of the public
            # contract on the returned server object. Cast at the
            # access boundary; the alternative (``getattr`` + None
            # checks) would obscure what's actually a stable
            # documented attribute.
            sockets = cast("asyncio.base_events.Server", site._server).sockets
            if sockets:
                port = sockets[0].getsockname()[1]
        return runner, identity, port

    def run(self) -> None:
        """Start the HTTP server (blocking)."""
        # Logging is already configured by __main__.py
        settings = self.settings
        # Fail-secure on the HA add-on path. The legacy dashboard
        # had a supervisor ``/auth`` fallback that gated the public
        # port with HA credentials when ``PASSWORD`` wasn't set; we
        # don't carry that forward (see issue #85). Without the
        # fallback, binding the public port without
        # ``USERNAME``/``PASSWORD`` would leave the dashboard
        # wide-open on the LAN whenever the add-on's ``ports:``
        # mapping exposed it. So when on-ha-addon and no password
        # is configured, run ingress-only and tell the operator
        # loudly how to enable LAN access if they want it.
        if settings.on_ha_addon and not settings.using_password:
            if not settings.create_ingress_site:
                # ``DISABLE_HA_AUTHENTICATION`` forces all traffic
                # through the public port (no trusted ingress site)
                # — but we have no credentials to gate it. Refuse
                # to start rather than expose an unauthenticated
                # dashboard. The supervisor surfaces this in the
                # add-on log so the operator sees exactly what to
                # change.
                msg = (
                    "Refusing to start: --ha-addon is set, "
                    "DISABLE_HA_AUTHENTICATION forces public-port auth, "
                    "and USERNAME/PASSWORD is not configured. Set "
                    "USERNAME and PASSWORD via the add-on options, or "
                    "unset DISABLE_HA_AUTHENTICATION to use ingress-only "
                    "mode."
                )
                raise RuntimeError(msg)
            _LOGGER.warning(
                "Public port %d NOT bound: --ha-addon is set but "
                "USERNAME/PASSWORD is not configured. Running "
                "ingress-only — the dashboard works through the Home "
                "Assistant UI. To enable LAN access on port %d, set "
                "USERNAME and PASSWORD via the add-on options.",
                settings.port,
                settings.port,
            )
            app = self.create_app(trusted=True, with_ingress_site=False)
            web.run_app(
                app,
                host=settings.ingress_host or "0.0.0.0",
                port=settings.ingress_port,
            )
            return
        app = self.create_app()
        web.run_app(app, host=settings.host, port=settings.port)

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
    def _register_frontend(
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
                        _IMMUTABLE_HEADERS if _HASHED_FILENAME_RE.search(tail) else shell_headers
                    )
                    return web.FileResponse(resolved, headers=headers)
            # 404 asset-shaped requests instead of returning the SPA
            # shell so the browser doesn't try to parse HTML as JS /
            # CSS / etc. on a hard-reload of a deep URL — see
            # ``_ASSET_EXTENSIONS`` for the rationale.
            if tail and Path(tail).suffix.lower() in _ASSET_EXTENSIONS:
                raise web.HTTPNotFound()
            return _render_shell(request, tail=tail)

        app.router.add_static("/assets", assets_dir)
        app.router.add_get("/", handle_index)
        app.router.add_get("/{tail:.*}", handle_spa)

        _LOGGER.info("Serving frontend from %s (dev_mode=%s)", frontend_dir, dev_mode)
