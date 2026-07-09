"""Bind / teardown / rebuild of the remote-build peer-link Noise-XX listener."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import web

from .api.ws import init_ws_app
from .constants import REMOTE_BUILD_PORT_SCAN_ATTEMPTS
from .controllers.config import (
    has_remote_build_settings_persisted,
    load_remote_build_settings,
)
from .controllers.remote_build.peer_link import PEER_LINK_PATH, make_peer_link_handler
from .helpers.async_ import run_in_executor
from .helpers.network_interfaces import (
    bind_available_port,
    ensure_single_host_for_ephemeral_port,
    resolve_bind_host,
)

if TYPE_CHECKING:
    from .device_builder import DeviceBuilder
    from .helpers.peer_link_identity import PeerLinkIdentity

_LOGGER = logging.getLogger(__name__)


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


class RemoteBuildLifecycle:
    """Remote-build peer-link listener lifecycle, composed into ``DeviceBuilder``."""

    def __init__(self, db: DeviceBuilder) -> None:
        """Bind to the owning ``DeviceBuilder``; the listener starts unbound."""
        self._db = db
        # Peer-link Noise WS receiver site for
        # ``/remote-build/peer-link`` (issue #106). Bound only when
        # ``RemoteBuildSettings.enabled`` is true; ``None`` otherwise.
        self._runner: web.AppRunner | None = None
        # Serialises listener-state mutations so two clients
        # toggling ``set_settings`` (or a ``rotate_identity`` racing a
        # toggle) can't interleave their teardown + rebind sequences.
        # Lazy-init at first acquire so the lock binds to the running
        # event loop, not the loop that ran ``DeviceBuilder.__init__``.
        self._lifecycle_lock: asyncio.Lock | None = None

    @property
    def is_listener_bound(self) -> bool:
        """True iff the remote-build peer-link Noise WS listener is currently bound."""
        return self._runner is not None

    async def maybe_start(self) -> None:
        """
        Bind the peer-link Noise WS listener if remote-build is enabled.

        Default-on for non-HA-addon deployments: a fresh sidecar
        deserialises to ``RemoteBuildSettings(enabled=True)`` and
        the listener binds without an extra operator step. The
        receiver-side **pair-approval dialog** is the privilege
        gate — an unpaired peer can connect to the TCP port but
        the Noise XX handshake fails without a matching pubkey, so
        binding the port grants nothing on its own. Loads the
        X25519 peer-link identity through
        :attr:`DeviceBuilder.peer_link_identity_store` — the sole
        cryptographic identity used by this listener; the store
        caches the identity so repeated binds don't re-read the
        keypair file.

        **HA addon: default-off but operator-overridable.** The
        addon's docker container doesn't expose port 6055 to the
        LAN by default, and the mDNS advertise is already skipped
        on HA addon — so binding by default would produce a port
        that's invisible to LAN peers. But some legacy-dashboard
        users DID expose port 6052 (and historically other addon
        ports) via the addon's ``ports:`` config, so a hard skip
        would lock them out. The compromise: on HA addon, skip
        the bind unless the operator has *explicitly persisted*
        ``_remote_build`` in metadata via the Settings toggle.
        ``has_remote_build_settings_persisted`` returns ``True``
        the moment ``set_settings`` writes the block — even a
        write that lands on the dataclass defaults still flips
        the signal. This means: fresh addon install → no bind;
        addon operator flips the toggle in Settings → bind
        respects the persisted ``enabled`` field. The HA-addon
        operator path stays open; the fresh-install default
        stops burning a port nothing can reach.

        Fail-soft: any exception during identity load or bind is
        caught and logged. The main dashboard keeps running; the
        operator gets a warning and the listener is simply absent
        until the next restart with the issue resolved.
        """
        if self._db.remote_build_receiver is None or self._db.loop is None:
            return
        settings = self._db.settings
        if settings.on_ha_addon:
            persisted = await run_in_executor(
                has_remote_build_settings_persisted, settings.config_dir
            )
            if not persisted:
                _LOGGER.debug(
                    "Skipping remote-build peer-link site: running as HA addon "
                    "without an explicit ``_remote_build`` block in metadata "
                    "(addon container doesn't expose port 6055 to the LAN by "
                    "default; flip the toggle in Settings to override)"
                )
                return
        rb_settings = await run_in_executor(load_remote_build_settings, settings.config_dir)
        if not rb_settings.enabled:
            # ``--remote-build-only`` has no dashboard UI to flip the
            # persisted toggle back on, and a receiver with no listener
            # is pointless — the CLI mode overrides the sidecar.
            if not settings.remote_build_only:
                _LOGGER.debug(
                    "Skipping remote-build peer-link site: disabled in settings "
                    "(set ``remote_build/set_settings`` enabled=true to bind)"
                )
                return
            _LOGGER.info(
                "Remote-build is disabled in the persisted settings but "
                "--remote-build-only forces the peer-link listener on"
            )

        try:
            runner, identity, port = await self._build_and_start_runner()
        except Exception:
            _LOGGER.exception(
                "Remote-build peer-link site failed to start; dashboard continues "
                "without the receiver listener. Disable in Settings or "
                "fix the underlying error and restart."
            )
            return
        self._runner = runner

        # Update the mDNS advertise AFTER the bind succeeds. If the
        # bind raised (port in use, permission denied, ...) the
        # advertiser stays at its pre-listener state instead of
        # broadcasting a pin + port that nothing's actually
        # listening on.
        await self.publish_advertise(
            pin_sha256=identity.pin_sha256,
            remote_build_port=port,
        )

        _LOGGER.info(
            "Remote-build peer-link site listening on %s:%d (peer-link pin %s)",
            settings.remote_build_host,
            port,
            identity.pin_sha256_formatted,
        )

    async def publish_advertise(
        self,
        *,
        pin_sha256: str | None,
        remote_build_port: int | None,
    ) -> None:
        """
        Push pin / port updates to the mDNS advertise, fail-soft on refresh.

        Centralises the setter-then-refresh dance shared by
        :meth:`maybe_start` (post-bind: real pin + port) and
        :meth:`reload_identity` (post-teardown: ``None`` + ``None``
        to clear both fields out of TXT until the rebuild
        succeeds). Both fields are always updated; the contract is
        "``pin_sha256`` and ``remote_build_port`` appear in TXT iff
        the listener is currently bound", so peers re-browsing while
        the listener is down see neither field and don't try to
        connect to a port that's no longer serving traffic. The
        explicit ``refresh`` call republishes the ServiceInfo if any
        TXT property changed; without it the setter-driven update
        would only land on the wire on the next periodic refresh
        tick (5 min). A flaky zeroconf refresh is logged and
        swallowed so caller paths (bind, rotate) don't fail just
        because the responder is wedged.

        No-op when no advertiser is attached.
        """
        advertiser = self._db.dashboard_advertiser
        if advertiser is None:
            return
        advertiser.set_pin_sha256(pin_sha256)
        advertiser.set_remote_build_port(remote_build_port)
        try:
            await advertiser.refresh()
        except Exception:
            # Fail-soft: a wedged responder shouldn't take down the
            # bind / rotate path. Log (with traceback) so a
            # chronically failing refresh — peers stuck on stale or
            # absent pin/port TXT — is diagnosable rather than silent.
            _LOGGER.warning(
                "Remote-build mDNS advertise refresh failed; the pin/port TXT "
                "update may not have reached the wire until the next periodic "
                "refresh tick",
                exc_info=True,
            )

    async def apply_enabled(self) -> bool:
        """
        Converge the peer-link listener to the on-disk ``enabled`` flag.

        Called by ``ReceiverController.set_settings`` after the
        new ``enabled`` value lands on disk. Reads back from disk
        under the lifecycle lock so the last-writer-wins persisted
        value is always what the listener converges to — two
        clients flipping ``enabled`` concurrently can't desync disk
        from listener state.

        On disk ``enabled=True`` with the listener absent, runs the
        same path :meth:`maybe_start` does at startup (load X25519
        peer-link identity, bind the plain-TCP listener, push pin + port
        to mDNS). Fail-soft on bind error — the dashboard keeps
        running without a listener, and a subsequent
        ``set_settings`` retry can clear a transient port conflict
        without a restart.

        On disk ``enabled=False`` with the listener bound, tears
        down the runner and clears ``pin_sha256`` + ``remote_build_port``
        from mDNS via :meth:`_teardown_runner`.

        Returns whether the listener is bound after this call.
        """
        if self._db.loop is None:
            return self._runner is not None
        async with self._get_lock():
            rb_settings = await run_in_executor(
                load_remote_build_settings, self._db.settings.config_dir
            )
            if rb_settings.enabled:
                if self._runner is None:
                    await self.maybe_start()
            else:
                await self._teardown_runner()
            return self._runner is not None

    async def reload_identity(self) -> bool:
        """
        Rebuild the peer-link listener after an X25519 identity rotation.

        No-op when the listener isn't bound: the rotated key is
        already on disk and the next bind picks it up. When bound,
        tears the runner down — clearing pin + port from mDNS — then
        rebuilds via :meth:`maybe_start`, which loads the new key and
        re-pushes pin + port. Clear runs BEFORE rebuild so a rebuild
        failure leaves the cleared TXT as the steady state. Fail-soft;
        returns whether the listener is bound after this call.
        """
        async with self._get_lock():
            if self._runner is None:
                return False
            # ``_teardown_runner`` clears the advertise too, so peers
            # re-browsing during the rebuild window — or after a
            # rebuild failure — don't see stale pin + port pointing at
            # a listener that isn't there. ``maybe_start`` re-pushes
            # both on a successful rebuild.
            await self._teardown_runner()
            await self.maybe_start()
            return self._runner is not None

    async def shutdown(self) -> None:
        """
        Tear down the listener at dashboard stop.

        Acquires the lifecycle lock so a concurrent
        :meth:`apply_enabled` / :meth:`reload_identity` can't
        interleave its rebind with this teardown — without the
        lock, an in-flight toggle could land a fresh runner *after*
        ``stop()`` has cleared the slot, leaking a listener past
        shutdown. Does NOT clear the mDNS advertise: the caller
        (``DeviceBuilder.stop``) unregisters the whole advertiser
        immediately after, so a TXT-only clear would be wasted work
        racing the unregister.
        """
        async with self._get_lock():
            if self._runner is None:
                return
            old_runner = self._runner
            self._runner = None
            await self._cleanup_runner(old_runner)

    def _get_lock(self) -> asyncio.Lock:
        """Lazy-init the lock against the running loop on first acquire."""
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        return self._lifecycle_lock

    async def _teardown_runner(self) -> None:
        """
        Stop the bound peer-link listener and clear its mDNS advertise.

        Caller MUST hold the lifecycle lock. No-op when the listener
        isn't bound. Sequencing matters: the runner reference is
        cleared *before* awaiting cleanup so a concurrent
        listener-state observer sees the steady "absent" state from
        the moment we commit to teardown, and the mDNS clear runs
        *after* cleanup so peers re-browsing during the window get a
        TXT without ``pin_sha256`` / ``remote_build_port`` the moment
        the port stops serving traffic.
        """
        if self._runner is None:
            return
        old_runner = self._runner
        self._runner = None
        await self._cleanup_runner(old_runner)
        await self.publish_advertise(
            pin_sha256=None,
            remote_build_port=None,
        )

    @staticmethod
    async def _cleanup_runner(runner: web.AppRunner) -> None:
        """
        Clean up a runner, logging (not raising) on failure.

        A failed ``AppRunner.cleanup`` (socket not released,
        lingering connections) would otherwise leak the listener
        socket and block the next bind on a fixed port. Swallow the
        exception so teardown stays fail-soft, but log it with a
        traceback so the leak is observable in production instead of
        invisible.
        """
        try:
            await runner.cleanup()
        except Exception:
            _LOGGER.warning(
                "Remote-build peer-link listener cleanup failed; a leaked "
                "socket may block the next bind on the configured port",
                exc_info=True,
            )

    async def _build_and_start_runner(
        self,
    ) -> tuple[web.AppRunner, PeerLinkIdentity, int]:
        """
        Construct the runner and bind the peer-link Noise WS listener.

        Loads the X25519 peer-link identity and binds a
        plain-TCP listener serving exactly one route: the WS upgrade
        at ``/remote-build/peer-link``. Noise XX provides
        confidentiality + mutual auth + forward secrecy at the
        application layer, so there's no SSL context to manage.

        Returns ``(runner, identity, bound_port)`` on success; on
        any exception, cleans up the partial runner before
        re-raising so the caller's ``except`` only has to log +
        return.

        ``bound_port`` is read off the bound socket, never assumed:
        it is the OS-assigned port for ``--remote-build-port 0``
        (ephemeral), and may exceed the configured port when that
        port was taken and the bind fell forward to the next free
        one (bounded scan of ``REMOTE_BUILD_PORT_SCAN_ATTEMPTS``
        candidates, each free on every bind host).

        Bind address comes from
        :attr:`DashboardSettings.remote_build_host` (``0.0.0.0`` by
        default) rather than the HTTP/WS dashboard's
        :attr:`~DashboardSettings.host`. The desktop app shape
        passes ``--host 127.0.0.1`` for the dashboard's loopback
        security model, but the peer-link still needs to be
        LAN-reachable so paired peers can dial the IPs the mDNS
        announce broadcasts (the announce carries every non-loopback
        adapter address). The peer-link's security gate is Noise +
        pre-shared pin, so binding to all interfaces by default is
        the right behaviour. Operators who want to lock the receiver
        to a specific NIC can override via ``--remote-build-host`` /
        ``$ESPHOME_REMOTE_BUILD_HOST``.
        """
        receiver = self._db.remote_build_receiver
        assert receiver is not None  # caller-checked
        settings = self._db.settings

        # Validate before acquiring resources so the caller's
        # fail-soft handler logs cleanly. The mDNS ``remote_build_port``
        # TXT field only carries one port, so a multi-host expansion
        # combined with an ephemeral port has no safe answer.
        configured_port = settings.remote_build_port
        hosts = resolve_bind_host(settings.remote_build_host)
        ensure_single_host_for_ephemeral_port(hosts, configured_port, "--remote-build-port")

        # Bind (and keep) the sockets up front: holding them is what
        # makes the reservation race-free against sibling instances
        # (e.g. the stable/beta/dev add-on flavors) scanning the same
        # range at startup; ``web.SockSite`` below adopts them.
        bind_port, bound_sockets = await run_in_executor(
            bind_available_port,
            hosts,
            configured_port,
            REMOTE_BUILD_PORT_SCAN_ATTEMPTS,
        )
        if configured_port and bind_port != configured_port:
            _LOGGER.warning(
                "Remote-build peer-link port %d is in use (likely another "
                "dashboard instance on this host); falling forward to port "
                "%d — peers discover the bound port via the mDNS "
                "remote_build_port TXT record",
                configured_port,
                bind_port,
            )

        runner: web.AppRunner | None = None
        try:
            identity = await self._db.peer_link_identity_store.async_load()
            app = web.Application(middlewares=[_strip_server_header_middleware])
            # Same WS init shape as the main /ws app: seed the
            # active-WS registry + the shutdown closer so an idle
            # paired offloader doesn't pin ``runner.cleanup()``
            # to aiohttp's 60s ``shutdown_timeout`` while its
            # handler sits in ``async for msg in session.ws``.
            init_ws_app(app)
            handler = make_peer_link_handler(receiver, identity)
            app.router.add_get(PEER_LINK_PATH, handler)

            runner = web.AppRunner(app)
            await runner.setup()
            # A started SockSite hands socket ownership to the
            # runner; the except path below closes any socket a
            # failed start left behind (double-close is a no-op).
            for sock in bound_sockets:
                await web.SockSite(runner, sock).start()
        except Exception:
            for sock in bound_sockets:
                sock.close()
            if runner is not None:
                await self._cleanup_runner(runner)
            raise

        return runner, identity, bind_port
