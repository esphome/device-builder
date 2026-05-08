"""
Remote-build feature — peer dashboard discovery + settings.

Phase 2 of issue #106. Browses ``_esphomebuilder._tcp.local.`` to
list other dashboards reachable on the LAN, and persists the
receiver-side ``enabled`` master switch the rest of the feature
will gate behind in phase 3.

Phase 2 stops at discovery + settings storage:

* No HTTP / WS endpoints under ``/remote-build/v1/*`` yet (phase 3
  lands the auth middleware + cert).
* No pairing or peer-link WS yet (phase 4 / phase 5).
* The ``enabled`` setting is persisted but not wired to any
  endpoint registration — flipping it currently has no observable
  effect beyond round-tripping in the settings UI. That's
  deliberate scaffolding so phase 3+ have a place to plug in.

Browser uses the existing ``AsyncEsphomeZeroconf`` instance owned by
:class:`~esphome_device_builder.controllers._device_state_monitor.DeviceStateMonitor`
— the dashboard ships one mDNS responder per process, and this
controller adds a second :class:`~zeroconf.asyncio.AsyncServiceBrowser`
on the same instance for the new service type. The state monitor's
own browsers (``_esphomelib._tcp.local.`` for devices,
``_http._tcp.local.`` for adoptable web UIs) are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ..helpers.api import CommandError, api_command
from ..helpers.dashboard_advertise import SERVICE_TYPE
from ..models import ErrorCode, RemoteBuildPeer, RemoteBuildSettings
from .config import load_remote_build_settings, save_remote_build_settings

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

# Timeout for the cache-miss resolve path. Longer than
# ``DeviceStateMonitor._MDNS_RESOLVE_TIMEOUT_MS`` (2s) because peer
# dashboards typically run on full hosts (laptop, desktop, addon
# container) that may be a few hops further away on the LAN than
# an ESPHome device, and the user-visible cost of a slow first
# discovery is "the peer doesn't appear in Settings for a few
# seconds" — not the device-state miss the shorter timeout
# protects against.
_RESOLVE_TIMEOUT_MS = 3000


def _decode_txt_value(raw: bytes | None) -> str:
    """Decode a TXT value as UTF-8, falling back to the empty string."""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _peer_from_service_info(name: str, info: AsyncServiceInfo) -> RemoteBuildPeer:
    """
    Build a :class:`RemoteBuildPeer` from a resolved ``AsyncServiceInfo``.

    Keeps the parsing in one place so ``_apply_service_info`` and
    the cache-hit branch produce identical shapes.

    Uses ``parsed_scoped_addresses(IPVersion.All)`` rather than
    ``parsed_addresses()`` so IPv6 link-local entries keep their
    ``%<interface>`` scope suffix. Without the scope, an
    ``fe80::xxx`` address parses but isn't connectable — the OS
    needs to know which interface to send the packet out on.
    Mirrors the choice already made in
    :class:`DeviceStateMonitor` (line 901).
    """
    properties = info.properties or {}
    server_version = _decode_txt_value(properties.get(b"server_version"))
    esphome_version = _decode_txt_value(properties.get(b"esphome_version"))
    # ``info.name`` comes back as ``<instance>.<service_type>``; we
    # only want the leftmost label as the friendly name.
    instance = (info.name or name).split(".", 1)[0]
    server = info.server or ""
    return RemoteBuildPeer(
        name=instance,
        hostname=server,
        port=info.port or 0,
        addresses=info.parsed_scoped_addresses(IPVersion.All) or [],
        server_version=server_version,
        esphome_version=esphome_version,
    )


class RemoteBuildController:
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
        self._peers: dict[str, RemoteBuildPeer] = {}
        # Strong refs for fire-and-forget resolve tasks so the
        # garbage collector can't reap them mid-await.
        self._tasks: set[asyncio.Task[None]] = set()
        # The mDNS service-instance name our own ``DashboardAdvertiser``
        # publishes — captured at start so we can filter our own
        # broadcast out of the discovered list. ``None`` when the
        # advertiser was skipped (HA addon mode, zeroconf failed) —
        # in that case there's nothing to filter.
        self._own_instance_name: str | None = None

    async def start(self) -> None:
        """
        Wire the browser onto the shared zeroconf and capture self-name.

        No-op when zeroconf failed to start — peer discovery is a
        nice-to-have, not load-bearing, and the controller stays in
        a "no peers, never will be" state until the next dashboard
        restart. Same fail-soft contract as
        :class:`DashboardAdvertiser`.
        """
        if self._db.devices is None:
            _LOGGER.debug("RemoteBuildController.start called before devices controller")
            return
        zeroconf = self._db.devices.zeroconf
        if zeroconf is None:
            _LOGGER.debug("zeroconf unavailable — remote-build discovery disabled")
            return
        # Capture own service-instance name so our own advertise
        # doesn't show up in ``list_hosts``. Reads through the
        # public ``service_instance_name`` accessor on
        # ``DashboardAdvertiser`` rather than reaching into
        # ``_info`` — keeps this controller decoupled from the
        # advertiser's private layout.
        advertiser = self._db._dashboard_advertiser
        if advertiser is not None:
            self._own_instance_name = advertiser.service_instance_name
        # Wrap browser construction so a zeroconf-side failure (e.g.
        # the underlying socket got torn down between
        # ``DeviceStateMonitor.start`` and now, or the cache is in an
        # unexpected state) doesn't abort dashboard startup. Peer
        # discovery is fail-soft — same contract as the advertise.
        try:
            self._browser = AsyncServiceBrowser(
                zeroconf.zeroconf,
                [SERVICE_TYPE],
                handlers=[self._on_service_state_change],
            )
        except Exception:
            _LOGGER.exception("Could not start remote-build browser — peer discovery disabled")
            self._browser = None

    async def stop(self) -> None:
        """Cancel the browser and drain in-flight resolve tasks."""
        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception:
                _LOGGER.debug("remote-build browser cancel failed", exc_info=True)
            self._browser = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        self._peers.clear()

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
        Browser callback — resolve the service info and update the peer map.

        Filters our own service-instance name so the advertise we
        publish doesn't show up in ``list_hosts``. ``Removed`` events
        delete the peer immediately; ``Added`` / ``Updated`` resolve
        either from the zeroconf cache (sync) or via a fire-and-forget
        task (async).
        """
        if name == self._own_instance_name:
            return
        if state_change == ServiceStateChange.Removed:
            self._peers.pop(name, None)
            return
        info = AsyncServiceInfo(service_type, name)
        if info.load_from_cache(zeroconf):
            self._peers[name] = _peer_from_service_info(name, info)
            return
        task = asyncio.create_task(self._resolve_and_apply(zeroconf, info, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve_and_apply(self, zeroconf: Any, info: AsyncServiceInfo, name: str) -> None:
        """Async resolve path for cache misses."""
        try:
            resolved = await info.async_request(zeroconf, timeout=_RESOLVE_TIMEOUT_MS)
        except Exception:
            _LOGGER.debug("Resolve failed for %s", name, exc_info=True)
            return
        if not resolved:
            return
        self._peers[name] = _peer_from_service_info(name, info)

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------

    @api_command("remote_build/list_hosts")
    async def list_hosts(self, **kwargs: Any) -> list[RemoteBuildPeer]:
        """
        Return every peer dashboard discovered on the LAN.

        Snapshot of the browser's state — fresh for every call, no
        caching. Empty when the browser hasn't seen any peers yet
        (or when zeroconf failed to start). Phase 3+ will add
        paired-or-not state to each row; phase 2 just returns the
        raw discovery.
        """
        return list(self._peers.values())

    @api_command("remote_build/get_settings")
    async def get_settings(self, **kwargs: Any) -> RemoteBuildSettings:
        """Return the receiver-side remote-build settings."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )

    @api_command("remote_build/set_settings")
    async def set_settings(self, *, enabled: bool, **kwargs: Any) -> RemoteBuildSettings:
        """
        Persist the receiver-side remote-build settings.

        Currently only ``enabled`` is settable; future phases add
        knobs from the "Remote builder settings" section of the
        design (artifact retention TTL, build target preference,
        major-version-mismatch toggle, ...). Returns the
        post-write value so the frontend can confirm the round-trip.

        Validates ``enabled`` is strictly a ``bool`` rather than
        coercing truthiness — a client sending the string ``"false"``
        for example would otherwise persist as ``True``, which is
        the opposite of what the user intended on a security-
        sensitive toggle.
        """
        if not isinstance(enabled, bool):
            msg = "remote_build/set_settings: 'enabled' must be a boolean"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        settings = RemoteBuildSettings(enabled=enabled)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, save_remote_build_settings, self._db.settings.config_dir, settings
        )
        return settings
