"""
Remote-build feature; peer dashboard discovery + settings.

Phase 2 / 2b of issue #106. Browses ``_esphomebuilder._tcp.local.``
to list other dashboards reachable on the LAN; persists the
receiver-side ``enabled`` master switch (phase 2) and the
user-supplied manual-host list for cross-subnet / non-multicast
LANs (phase 2b); merges both sources into a single
``remote_build/list_hosts`` snapshot.

Phase 2 / 2b stops at discovery + settings storage:

* No HTTP / WS endpoints under ``/remote-build/v1/*`` yet (phase 3
  lands the auth middleware + cert).
* No pairing or peer-link WS yet (phase 4 / phase 5).
* The ``enabled`` setting is persisted but not wired to any
  endpoint registration; flipping it currently has no observable
  effect beyond round-tripping in the settings UI. That's
  deliberate scaffolding so phase 3+ have a place to plug in.
* Manual hosts have no version / fingerprint resolution; they
  land in ``list_hosts`` with empty ``server_version`` /
  ``esphome_version`` until phase 4 attempts the connection.

Browser uses the existing ``AsyncEsphomeZeroconf`` instance owned by
:class:`~esphome_device_builder.controllers._device_state_monitor.DeviceStateMonitor`,
so the dashboard ships one mDNS responder per process and this
controller adds a second :class:`~zeroconf.asyncio.AsyncServiceBrowser`
on the same instance for the new service type. The state monitor's
own browsers (``_esphomelib._tcp.local.`` for devices,
``_http._tcp.local.`` for adoptable web UIs) are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ..helpers.api import CommandError, api_command
from ..helpers.dashboard_advertise import SERVICE_TYPE
from ..models import (
    ErrorCode,
    ManualHost,
    RemoteBuildPeer,
    RemoteBuildPeerSource,
    RemoteBuildSettings,
)
from .config import load_remote_build_settings, remote_build_settings_transaction

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

# Timeout for the cache-miss resolve path. Longer than
# ``DeviceStateMonitor._MDNS_RESOLVE_TIMEOUT_MS`` (2s) because peer
# dashboards typically run on full hosts (laptop, desktop, addon
# container) that may be a few hops further away on the LAN than
# an ESPHome device, and the user-visible cost of a slow first
# discovery is "the peer doesn't appear in Settings for a few
# seconds"; not the device-state miss the shorter timeout
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
    ``fe80::xxx`` address parses but isn't connectable; the OS
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
        source=RemoteBuildPeerSource.MDNS,
        addresses=info.parsed_scoped_addresses(IPVersion.All) or [],
        server_version=server_version,
        esphome_version=esphome_version,
    )


def _peer_from_manual_host(entry: ManualHost) -> RemoteBuildPeer:
    """
    Build a :class:`RemoteBuildPeer` from a stored :class:`ManualHost`.

    Manual hosts skip resolution; phase 2b just surfaces the
    user-entered ``(hostname, port)`` so the frontend can render
    the row alongside mDNS-discovered ones. Phase 4 attempts the
    actual connection and fills the version fields.

    ``name`` is the hostname verbatim (rather than the leftmost
    label) so an IP-only entry still reads sensibly in the UI;
    the frontend can render a "Manual" badge to distinguish it
    from an mDNS-discovered row.
    """
    return RemoteBuildPeer(
        name=entry.hostname,
        hostname=entry.hostname,
        port=entry.port,
        source=RemoteBuildPeerSource.MANUAL,
    )


def _validate_hostname(raw: object) -> str:
    """
    Normalise a user-entered hostname to its canonical lowercase form.

    Rejects non-string and empty / whitespace-only input with
    :class:`CommandError(INVALID_ARGS)`. Lowercase normalisation
    matches the duplicate-check semantics; hostnames are
    case-insensitive per RFC 1035 §2.3.3, so ``Desktop.local`` and
    ``desktop.local`` should be the same entry. The stored form
    is the trimmed, lowercased string (so two adds with different
    casing collapse to one entry rather than registering twice).
    Phase 4 attempts the actual connection (and discovers DNS /
    TLS validity); phase 2b deliberately doesn't pre-flight an
    "is this resolvable now?" check, which would fail on offline
    laptops adding a peer for later.
    """
    if not isinstance(raw, str):
        msg = "manual host: 'hostname' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip().lower()
    if not trimmed:
        msg = "manual host: 'hostname' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


def _validate_port(raw: object) -> int:
    """
    Validate a user-entered port number.

    ``bool`` is rejected even though ``isinstance(True, int)`` is
    true; accepting ``True`` for a port number is a footgun
    (silently coerces to 1, which IANA reserves for tcpmux).
    Range is the IANA-registered ephemeral plus
    well-known: 1-65535.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        msg = "manual host: 'port' must be an integer"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not 1 <= raw <= 65535:
        msg = "manual host: 'port' must be between 1 and 65535"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return raw


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
        # publishes; captured at start so we can filter our own
        # broadcast out of the discovered list. ``None`` when the
        # advertiser was skipped (HA addon mode, zeroconf failed),
        # in which case there's nothing to filter.
        self._own_instance_name: str | None = None

    async def start(self) -> None:
        """
        Wire the browser onto the shared zeroconf and capture self-name.

        No-op when zeroconf failed to start; peer discovery is a
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
            _LOGGER.debug("zeroconf unavailable; remote-build discovery disabled")
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
        # Wrap browser construction so a zeroconf-side failure (e.g.
        # the underlying socket got torn down between
        # ``DeviceStateMonitor.start`` and now, or the cache is in an
        # unexpected state) doesn't abort dashboard startup. Peer
        # discovery is fail-soft; same contract as the advertise.
        try:
            self._browser = AsyncServiceBrowser(
                zeroconf.zeroconf,
                [SERVICE_TYPE],
                handlers=[self._on_service_state_change],
            )
        except Exception:
            _LOGGER.exception("Could not start remote-build browser; peer discovery disabled")
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
        Browser callback; resolve the service info and update the peer map.

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
        Return every peer dashboard known to this receiver.

        Merges two sources into a single snapshot:

        * mDNS-discovered peers from the browser (``source=MDNS``,
          full version + address info).
        * Manually-added peers from
          ``_remote_build.manual_hosts`` (``source=MANUAL``, blank
          version fields until phase 4 fills them in).

        Manual hosts are placed AFTER mDNS hits so the UI's
        primary content is the auto-discovered list. A
        manually-added entry that's also reachable via mDNS shows
        up twice for now (once per source); phase 4's pairing
        flow will introduce the deduplication logic alongside the
        actual connection attempt.
        """
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return [
            *self._peers.values(),
            *(_peer_from_manual_host(entry) for entry in settings.manual_hosts),
        ]

    @api_command("remote_build/get_settings")
    async def get_settings(self, **kwargs: Any) -> RemoteBuildSettings:
        """Return the receiver-side remote-build settings."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )

    async def _modify_settings(
        self, mutator: Callable[[RemoteBuildSettings], None]
    ) -> RemoteBuildSettings:
        """
        Run ``mutator`` against the current settings and persist the result.

        Wraps :func:`remote_build_settings_transaction` so the
        whole read-modify-write happens under the metadata lock,
        so two concurrent callers can't both read the same starting
        value and have the second save wipe the first's change.
        Runs in the default executor since the transaction does
        blocking JSON I/O.

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
        return await loop.run_in_executor(None, _txn)

    @api_command("remote_build/set_settings")
    async def set_settings(self, *, enabled: bool, **kwargs: Any) -> RemoteBuildSettings:
        """
        Persist the receiver-side ``enabled`` master switch.

        Read-modify-write so manual hosts and any future phase-3+
        fields stay intact; a client toggling just ``enabled``
        doesn't reset every other field to its default.

        Validates ``enabled`` is strictly a ``bool`` rather than
        coercing truthiness; a client sending the string ``"false"``
        for example would otherwise persist as ``True``, which is
        the opposite of what the user intended on a security-
        sensitive toggle.
        """
        if not isinstance(enabled, bool):
            msg = "remote_build/set_settings: 'enabled' must be a boolean"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        def _set(settings: RemoteBuildSettings) -> None:
            settings.enabled = enabled

        return await self._modify_settings(_set)

    # ------------------------------------------------------------------
    # Manual hosts (phase 2b)
    # ------------------------------------------------------------------

    @api_command("remote_build/add_manual_host")
    async def add_manual_host(
        self, *, hostname: str, port: int, **kwargs: Any
    ) -> RemoteBuildSettings:
        """
        Add a manually-entered peer for cross-subnet / non-mDNS LANs.

        Validates ``hostname`` (non-empty string, normalised to
        lowercase per RFC 1035 §2.3.3) and ``port`` (integer,
        1-65535). Rejects duplicates by ``(hostname, port)``:
        adding the same pair twice raises ``ALREADY_EXISTS`` so
        the frontend can render a "this dashboard is already in
        your list" message without string-matching the details
        field.

        Returns the post-write settings so the caller can re-render
        the manual-hosts list without a separate ``get_settings``
        round-trip.
        """
        host = _validate_hostname(hostname)
        port_num = _validate_port(port)

        def _add(settings: RemoteBuildSettings) -> None:
            for entry in settings.manual_hosts:
                if entry.hostname == host and entry.port == port_num:
                    msg = f"manual host {host}:{port_num} is already registered"
                    raise CommandError(ErrorCode.ALREADY_EXISTS, msg)
            settings.manual_hosts.append(ManualHost(hostname=host, port=port_num))

        return await self._modify_settings(_add)

    @api_command("remote_build/remove_manual_host")
    async def remove_manual_host(
        self, *, hostname: str, port: int, **kwargs: Any
    ) -> RemoteBuildSettings:
        """
        Remove a previously-added manual peer.

        Hostname normalisation matches :meth:`add_manual_host` so a
        case-different removal request finds the entry. A
        non-existent ``(hostname, port)`` pair raises
        ``NOT_FOUND`` so the caller knows the operation was a no-op
        rather than silently succeeding (matters for the
        Settings UI: "Removed Foo" toast vs no feedback).
        """
        host = _validate_hostname(hostname)
        port_num = _validate_port(port)

        def _remove(settings: RemoteBuildSettings) -> None:
            kept = [
                entry
                for entry in settings.manual_hosts
                if not (entry.hostname == host and entry.port == port_num)
            ]
            if len(kept) == len(settings.manual_hosts):
                msg = f"manual host {host}:{port_num} is not registered"
                raise CommandError(ErrorCode.NOT_FOUND, msg)
            settings.manual_hosts = kept

        return await self._modify_settings(_remove)
