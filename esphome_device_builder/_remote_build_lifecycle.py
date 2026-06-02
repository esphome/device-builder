"""
Remote-build peer-link listener lifecycle.

The :class:`_RemoteBuildLifecycleMixin` carries the bind / teardown /
rebuild of the Noise-XX peer-link receiver listener and its mDNS
advertise, mixed into
:class:`~esphome_device_builder.device_builder.DeviceBuilder` to keep
that module under the file-size cap. The runner reference
(``_remote_build_runner``) and lifecycle lock
(``_remote_build_lifecycle_lock``) are owned by ``DeviceBuilder``'s
``__init__``; this mixin is only ever mixed into ``DeviceBuilder`` and
is never instantiated standalone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from aiohttp import web

from .api.ws import init_ws_app
from .controllers.config import (
    has_remote_build_settings_persisted,
    load_remote_build_settings,
)
from .controllers.remote_build.peer_link import PEER_LINK_PATH, make_peer_link_handler
from .helpers.network_interfaces import ensure_single_host_for_ephemeral_port, resolve_bind_host

if TYPE_CHECKING:
    from .controllers.config import DashboardSettings
    from .controllers.remote_build import ReceiverController
    from .helpers.dashboard_advertise import DashboardAdvertiser
    from .helpers.peer_link_identity import PeerLinkIdentity, PeerLinkIdentityStore

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


class _RemoteBuildLifecycleMixin:
    """Remote-build peer-link listener lifecycle, mixed into ``DeviceBuilder``."""

    # Provided by ``DeviceBuilder.__init__`` — declared here so the
    # mixin type-checks in isolation. This mixin is never instantiated
    # on its own.
    if TYPE_CHECKING:
        settings: DashboardSettings
        loop: asyncio.AbstractEventLoop | None
        remote_build_receiver: ReceiverController | None
        peer_link_identity_store: PeerLinkIdentityStore
        _dashboard_advertiser: DashboardAdvertiser | None
        _remote_build_runner: web.AppRunner | None
        _remote_build_lifecycle_lock: asyncio.Lock | None

    async def _maybe_start_remote_build_site(self) -> None:
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
        :attr:`peer_link_identity_store` — the sole
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
        if self.remote_build_receiver is None or self.loop is None:
            return
        loop = self.loop
        if self.settings.on_ha_addon:
            persisted = await loop.run_in_executor(
                None, has_remote_build_settings_persisted, self.settings.config_dir
            )
            if not persisted:
                _LOGGER.debug(
                    "Skipping remote-build peer-link site: running as HA addon "
                    "without an explicit ``_remote_build`` block in metadata "
                    "(addon container doesn't expose port 6055 to the LAN by "
                    "default; flip the toggle in Settings to override)"
                )
                return
        rb_settings = await loop.run_in_executor(
            None, load_remote_build_settings, self.settings.config_dir
        )
        if not rb_settings.enabled:
            _LOGGER.debug(
                "Skipping remote-build peer-link site: disabled in settings "
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

        _LOGGER.info(
            "Remote-build peer-link site listening on %s:%d (peer-link pin %s)",
            self.settings.remote_build_host,
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

        Called by ``ReceiverController.set_settings`` after the
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
        Rebuild the peer-link listener after an X25519 identity rotation.

        Wired up to ``ReceiverController.rotate_identity`` right
        after :meth:`PeerLinkIdentityStore.async_rotate` writes
        the new X25519 keypair to disk. The new ``pin_sha256`` is what
        every paired offloader pins against on the next Noise
        handshake — the rotation invalidates every existing
        pairing, peers see a fingerprint mismatch and surface the
        re-pair wizard.

        When the listener is bound, three side effects in order:

        * Listener teardown — the bound runner is still holding
          the old X25519 peer-link identity in its handler closure.
          Without a rebuild, the next session would still drive
          the handshake against the old key.
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
          loads the new X25519 identity from disk and (on success)
          re-pushes the new pin + port to mDNS. Fail-soft: a
          rebuild failure leaves the dashboard running without a
          receiver listener (same contract as the initial bind),
          and the return value reflects that so the rotater can
          surface the failure to the operator.

        When the listener is NOT bound, this method is a no-op:
        no mDNS push (there's no listener for peers to connect
        to anyway, and pushing a pin without a port would
        contradict the TXT contract). The new X25519 key is
        already on disk by the time this method runs; the next
        bind picks it up.

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

        Loads the X25519 peer-link identity and binds a
        plain-TCP TCPSite serving exactly one route: the WS upgrade
        at ``/remote-build/peer-link``. Noise XX provides
        confidentiality + mutual auth + forward secrecy at the
        application layer, so there's no SSL context to manage.

        Returns ``(runner, identity, bound_port)`` on success; on
        any exception, cleans up the partial runner before
        re-raising so the caller's ``except`` only has to log +
        return.

        ``bound_port`` is the OS-assigned port when the operator
        passed ``--remote-build-port 0`` (ephemeral); otherwise the
        configured value verbatim. Reading the real port off the
        socket prevents mDNS / log lines from claiming port 0.

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
        loop = self.loop
        assert loop is not None  # caller-checked
        assert self.remote_build_receiver is not None  # caller-checked

        # Validate before acquiring resources so the caller's
        # fail-soft handler logs cleanly. The mDNS ``remote_build_port``
        # TXT field only carries one port, so a multi-host expansion
        # combined with an ephemeral port has no safe answer.
        configured_port = self.settings.remote_build_port
        hosts = resolve_bind_host(self.settings.remote_build_host)
        ensure_single_host_for_ephemeral_port(hosts, configured_port, "--remote-build-port")

        runner: web.AppRunner | None = None
        try:
            identity = await self.peer_link_identity_store.async_load()
            app = web.Application(middlewares=[_strip_server_header_middleware])
            # Same WS init shape as the main /ws app: seed the
            # active-WS registry + the shutdown closer so an idle
            # paired offloader doesn't pin ``runner.cleanup()``
            # to aiohttp's 60s ``shutdown_timeout`` while its
            # handler sits in ``async for msg in session.ws``.
            init_ws_app(app)
            handler = make_peer_link_handler(self.remote_build_receiver, identity)
            app.router.add_get(PEER_LINK_PATH, handler)

            runner = web.AppRunner(app)
            await runner.setup()
            # ``reuse_address=True`` is the asyncio default on POSIX
            # but defaults to False on Windows; pin it explicitly so
            # the rotation rebuild path
            # (``reload_remote_build_identity`` → teardown → re-bind)
            # doesn't TIME_WAIT-block on a fixed configured port
            # (default 6055) cross-platform. The ephemeral-port test
            # path masks this risk because the OS picks a fresh port
            # each rebuild; production deploys with a fixed port.
            for host in hosts:
                site = web.TCPSite(
                    runner,
                    host,
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
        # ``site._server`` is genuinely aiohttp-private — there's no
        # public way to get the bound port off a ``TCPSite`` after an
        # ephemeral-port (configured_port=0) bind. We reach in; if
        # aiohttp ever renames it the cast below crashes loudly.
        if configured_port == 0 and site._server is not None:  # noqa: SLF001
            # typeshed's ``asyncio.AbstractServer`` doesn't expose
            # ``sockets`` even though the concrete ``base_events.Server``
            # does — the asyncio docs list it as part of the public
            # contract on the returned server object. Cast at the
            # access boundary; the alternative (``getattr`` + None
            # checks) would obscure what's actually a stable
            # documented attribute.
            sockets = cast("asyncio.base_events.Server", site._server).sockets  # noqa: SLF001
            if sockets:
                port = sockets[0].getsockname()[1]
        return runner, identity, port
