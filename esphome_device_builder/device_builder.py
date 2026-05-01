"""ESPHome Device Builder — core application singleton.

The DeviceBuilder class is the main entry point. It owns controllers,
the event bus, and the aiohttp web application. Device state lives in
the DevicesController, not here.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .controllers.config import DashboardSettings
from .helpers.api import CommandHandler, collect_api_commands
from .helpers.auth import auth_middleware
from .helpers.event_bus import EventBus
from .helpers.json import cors_middleware

if TYPE_CHECKING:
    from .controllers.auth import AuthController
    from .controllers.automations import AutomationsController
    from .controllers.boards import BoardCatalog
    from .controllers.components import ComponentCatalog
    from .controllers.config import ConfigController
    from .controllers.devices import DevicesController
    from .controllers.editor import EditorController
    from .controllers.firmware import FirmwareController

_LOGGER = logging.getLogger(__name__)


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

    async def start(self) -> None:
        """Start the application — load catalogs, initialize controllers."""
        from .controllers.auth import AuthController
        from .controllers.automations import AutomationsController
        from .controllers.boards import BoardCatalog
        from .controllers.components import ComponentCatalog
        from .controllers.config import ConfigController
        from .controllers.devices import DevicesController
        from .controllers.editor import EditorController
        from .controllers.firmware import FirmwareController

        self.loop = asyncio.get_running_loop()

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
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self.devices is not None:
            await self.devices.stop()
        if self.editor is not None:
            await self.editor.stop()

    async def _run_background(self) -> None:
        """Background polling loop."""
        try:
            while True:
                await asyncio.sleep(5)
                if self.devices:
                    await self.devices.poll()
        except asyncio.CancelledError:
            pass

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
        as devices change. Subscription is active for the connection lifetime.
        """
        from .helpers.event_bus import Event
        from .models import EventType

        if client is None:
            return

        # Track pending tasks to prevent garbage collection
        pending_tasks: set[asyncio.Task] = set()

        def _on_event(event: Event) -> None:
            """Forward bus event to the WS client."""
            data = event.data
            serialized: dict[str, Any] = {}
            for key, value in data.items():
                serialized[key] = value.to_dict() if hasattr(value, "to_dict") else value
            task = asyncio.create_task(
                client.send_event(message_id, event.event_type.value, serialized)
            )
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        # Subscribe to all event types
        unsubscribers = []
        for event_type in EventType:
            unsub = self.bus.add_listener(event_type, _on_event)
            unsubscribers.append(unsub)

        # Send initial device list
        if self.devices:
            devices = self.devices.get_devices()
            await client.send_event(
                message_id,
                "initial_state",
                {"devices": [d.to_dict() for d in devices]},
            )

        # Confirm subscription
        await client.send_result(message_id, {"subscribed": True})

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

    def create_app(self, *, trusted: bool = False, with_lifecycle: bool = True) -> web.Application:
        """
        Build the aiohttp application.

        ``trusted`` skips the auth middleware (HA Ingress site).
        ``with_lifecycle`` toggles startup/cleanup hooks; the ingress
        app reuses the public app's controller singleton and so passes
        ``False`` to avoid re-initialising them.
        """
        middlewares: list[Any] = [cors_middleware]
        if not trusted:
            middlewares.append(auth_middleware)

        app = web.Application(middlewares=middlewares)
        app["device_builder"] = self
        app["trusted_site"] = trusted

        # WebSocket API
        from .api.ws import create_ws_routes

        app.router.add_routes(create_ws_routes())

        # Legacy REST endpoints (HA backward compat)
        from .api.legacy import create_legacy_routes

        app.router.add_routes(create_legacy_routes())

        # Static file serving for board images
        boards_dir = Path(__file__).parent / "definitions" / "boards"
        if boards_dir.is_dir():
            app.router.add_static("/boards/images", boards_dir)

        # Frontend serving
        frontend_dir = self._get_frontend_dir()
        if frontend_dir and frontend_dir.is_dir():
            self._register_frontend(app, frontend_dir)
        elif with_lifecycle:
            # The ingress app is silent here — the public app already logged.
            _LOGGER.info(
                "Frontend package not installed — running in API-only mode. "
                "Install esphome-device-builder-frontend for the web UI."
            )

        if with_lifecycle:
            app.on_startup.append(self._on_startup)
            if self.settings.create_ingress_site:
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
        app = self.create_app()
        web.run_app(app, host=self.settings.host, port=self.settings.port)

    @staticmethod
    def _get_frontend_dir() -> Path | None:
        """Return the path to the built frontend, or None if unavailable."""
        try:
            from esphome_device_builder_frontend import where  # type: ignore[import-not-found]

            return Path(where())
        except ImportError:
            return None

    @staticmethod
    def _register_frontend(app: web.Application, frontend_dir: Path) -> None:
        """Register static file routes for the built frontend."""
        index_html = frontend_dir / "index.html"

        async def handle_index(request: web.Request) -> web.FileResponse:
            return web.FileResponse(index_html)

        # FIFO route lookup means the explicit "/" handler wins for the
        # bare root, and the catch-all add_static under "/" picks up
        # everything else (hashed JS bundles, rspack license sidecars,
        # the assets/ tree). API routes are registered before this so
        # they continue to match first.
        app.router.add_get("/", handle_index)
        app.router.add_static("/", frontend_dir)

        _LOGGER.info("Serving frontend from %s", frontend_dir)
