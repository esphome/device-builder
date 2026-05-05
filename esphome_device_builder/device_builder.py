"""ESPHome Device Builder — core application singleton.

The DeviceBuilder class is the main entry point. It owns controllers,
the event bus, and the aiohttp web application. Device state lives in
the DevicesController, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from aiohttp import web

from .api.legacy import create_legacy_routes
from .api.ws import create_ws_routes
from .controllers.auth import AuthController
from .controllers.automations import AutomationsController
from .controllers.boards import BoardCatalog
from .controllers.components import ComponentCatalog
from .controllers.config import ConfigController, DashboardSettings
from .controllers.devices import DevicesController
from .controllers.editor import EditorController
from .controllers.firmware import FirmwareController
from .helpers.api import CommandHandler, collect_api_commands
from .helpers.auth import auth_middleware
from .helpers.event_bus import Event, EventBus, StreamControls, stream_events
from .helpers.json import cors_middleware
from .models import EventType

_LOGGER = logging.getLogger(__name__)

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

        # Command registry — populated from controllers
        self.command_handlers: dict[str, CommandHandler] = {}

        # Background tasks
        self._background_tasks: set[asyncio.Task] = set()
        self._bg_task: asyncio.Task | None = None

        self._ingress_runner: web.AppRunner | None = None

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
        await self.devices.start()
        await self.firmware.start()
        await self.editor.start()

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
        """Background polling loop."""
        while True:
            await asyncio.sleep(5)
            if self.devices:
                await self.devices.poll()

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
            # Importable devices are populated by the mDNS browser
            # and per-device events fire only on transitions; without
            # seeding the snapshot here a fresh page load misses
            # every importable device the dashboard had already seen
            # by then.
            if self.devices:
                devices = self.devices.get_devices()
                importable = self.devices.get_importable_devices()
                await client.send_event(
                    message_id,
                    "initial_state",
                    {
                        "devices": [d.to_dict() for d in devices],
                        "importable": [d.to_dict() for d in importable],
                    },
                )
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

        async def handle_index(request: web.Request) -> web.FileResponse:
            return web.FileResponse(index_html, headers=shell_headers)

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

        async def handle_spa(request: web.Request) -> web.FileResponse:
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
            return web.FileResponse(index_html, headers=shell_headers)

        app.router.add_static("/assets", assets_dir)
        app.router.add_get("/", handle_index)
        app.router.add_get("/{tail:.*}", handle_spa)

        _LOGGER.info("Serving frontend from %s (dev_mode=%s)", frontend_dir, dev_mode)
