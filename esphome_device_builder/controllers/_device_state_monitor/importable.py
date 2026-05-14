"""
Importable-discovery source: HTTP browser callbacks + adoption flow.

:class:`ImportableDiscovery` owns the upstream
``DashboardImportDiscovery`` instance, the ``_http._tcp.local.``
service-state callback used by the shared browser dispatch, the
``DiscoveredImport`` → ``AdoptableDevice`` translation, and the
public ``probe_device`` / ``revisit_*`` / ``get_importable_devices``
surface the dashboard's discovery banner reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from esphome.zeroconf import DashboardImportDiscovery, DiscoveredImport
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from ...models import AdoptableDevice
from .helpers import (
    _ESPHOME_SERVICE_TYPE,
    _HTTP_SERVICE_TYPE,
    _http_url_from_service_info,
    device_name_from_service,
)

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor


class ImportableDiscovery:
    """Importable / discovered-device flow owning the HTTP browser and adoption surface."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        self._import_discovery: DashboardImportDiscovery | None = None

    def setup(self) -> None:
        """Construct the upstream ``DashboardImportDiscovery``."""
        self._import_discovery = DashboardImportDiscovery(self._on_import_update)

    def browser_callback(
        self, zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        """Forward esphomelib browser events to the upstream DashboardImportDiscovery."""
        if self._import_discovery is not None:
            self._import_discovery.browser_callback(zeroconf, service_type, name, state_change)

    def probe_device(self, device_name: str, service_name: str | None = None) -> None:
        """Eagerly resolve a device's ``_esphomelib._tcp.local.`` service.

        Adoption / import / wizard-created devices land in the
        configured catalog the moment we write their YAML, but the
        regular browser path only updates ONLINE / IP / version /
        config_hash / api_encryption when the *next* mDNS announcement
        arrives — which can be minutes for a quiet device. This method
        short-circuits the wait by either reading the existing
        zeroconf cache (sync hit, common case for a device that was
        just on the discovery banner) or kicking off an
        ``async_request`` in a fire-and-forget task. Either way the
        apply path is the same one the browser uses, so the device's
        card flips from "Unknown" to a fully-populated card
        immediately instead of on the next periodic sweep.

        ``service_name`` defaults to ``device_name`` and is the
        broadcast name to look up in mDNS. Adoption surfaces a
        device whose mDNS-advertised name (the original factory
        firmware's hostname) differs from the user-chosen YAML name;
        passing it explicitly lets the lookup hit the cached service
        info while the apply still keys to the configured device's
        name.
        """
        monitor = self._monitor
        if (zc := monitor._mdns.zeroconf) is None:
            return
        zeroconf = zc.zeroconf
        broadcast = service_name or device_name
        full_service = f"{broadcast}.{_ESPHOME_SERVICE_TYPE}"
        info = AsyncServiceInfo(_ESPHOME_SERVICE_TYPE, full_service)
        if info.load_from_cache(zeroconf):
            monitor._mdns._apply_service_info(device_name, info)
            return
        monitor._track_task(monitor._mdns._resolve_and_apply(zeroconf, info, device_name))

    def revisit_importable(self, device_name: str) -> None:
        """
        Re-fire ``on_importable_added`` for *device_name* if upstream still has it cached.

        Used after a configured device is deleted: the device's mDNS
        announcement was being suppressed by the ``configured-name``
        filter in ``_on_import_update``, but upstream's
        ``DashboardImportDiscovery.import_state`` already has the
        ``DiscoveredImport`` entry from the original announcement.
        Without this nudge the discovery banner stays silent until the
        device re-announces (which can be minutes for a quiet device).

        Ignored devices are skipped — the user already said "don't
        show me this", so a deletion shouldn't unilaterally bring it
        back. They can unignore through the menu if they change their
        mind, and an unsolicited mDNS re-announce will surface it
        through the normal callback path either way.
        """
        if self._import_discovery is None or self._monitor._is_ignored(device_name):
            return
        for service_name, discovered in self._import_discovery.import_state.items():
            if discovered.device_name == device_name:
                self._on_import_update(service_name, discovered)

    def revisit_all_importables(self) -> None:
        """
        Re-fire ``on_importable_added`` for every cached importable.

        Used when a configured YAML is deleted but we don't know what
        mDNS name it came from (the user may have picked a YAML name
        that differs from the discovered hostname during adoption).
        ``_on_import_update`` already filters configured + ignored
        names so re-emitting the full set is safe; only the entries
        that should appear in the banner do.
        """
        if self._import_discovery is None:
            return
        for service_name, discovered in self._import_discovery.import_state.items():
            self._on_import_update(service_name, discovered)

    def get_importable_devices(self) -> list[AdoptableDevice]:
        """
        Snapshot of devices currently advertising as importable.

        Built fresh each call from ``DashboardImportDiscovery``'s
        ``import_state`` so the ``ignored`` flag and the configured-
        device filter both reflect the live dashboard state. Callers
        (e.g. the WebSocket ``initial_state`` event) get the same view
        the per-device ADDED events would have surfaced incrementally.
        """
        if self._import_discovery is None:
            return []
        configured_names = {d.name for d in self._monitor._get_devices()}
        out: list[AdoptableDevice] = []
        for discovered in self._import_discovery.import_state.values():
            if discovered.device_name in configured_names:
                continue
            out.append(self._build_adoptable(discovered))
        return out

    def on_http_service_state_change(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Track ``_http._tcp.local.`` services so discovered cards can show a Visit-web-UI link.

        The browser fires for every HTTP service on the LAN — we only
        care about the ones whose left-hand label matches an importable
        device, so the matching is name-driven. When an HTTP service
        appears (or disappears) for an existing importable, re-emit
        the entry so the card's ``web_url`` field stays in sync
        without waiting for the next esphomelib announcement.
        """
        monitor = self._monitor
        device_name = device_name_from_service(name)
        if state_change == ServiceStateChange.Removed:
            if monitor.state.http_urls.pop(device_name, None) is None:
                return
            self._refire_importable_for(device_name)
            return

        info = AsyncServiceInfo(service_type, name)
        if info.load_from_cache(zeroconf):
            self._apply_http_service_info(device_name, info)
            return
        monitor._track_task(self._resolve_and_apply_http(zeroconf, info, device_name))

    async def _resolve_and_apply_http(
        self, zeroconf: Any, info: AsyncServiceInfo, device_name: str
    ) -> None:
        """Resolve a cache-miss HTTP service and store its URL."""
        await self._monitor._mdns._resolve_then(
            zeroconf, info, device_name, self._apply_http_service_info
        )

    def _apply_http_service_info(self, device_name: str, info: AsyncServiceInfo) -> None:
        """Build the Visit-web-UI URL from a populated HTTP service info.

        Only stored when an importable device with the same name is
        currently advertising. Without this guard ``_http_urls`` grew
        unbounded from every HTTP service on the LAN (printers, NAS
        boxes, routers — none of which we have any use for); this
        keeps the cache scoped to entries that can actually drive a
        Visit-web-UI link on the discovered card.
        """
        if not self._has_importable(device_name):
            return
        url = _http_url_from_service_info(device_name, info)
        monitor = self._monitor
        if monitor.state.http_urls.get(device_name) == url:
            return
        monitor.state.http_urls[device_name] = url
        self._refire_importable_for(device_name)

    def _has_importable(self, device_name: str) -> bool:
        """Return True when an importable currently exists for *device_name*."""
        if self._import_discovery is None:
            return False
        return any(
            d.device_name == device_name for d in self._import_discovery.import_state.values()
        )

    def _refire_importable_for(self, device_name: str) -> None:
        """Re-emit ADDED for *device_name* so frontends pick up a web_url change."""
        if self._import_discovery is None:
            return
        for service_name, discovered in self._import_discovery.import_state.items():
            if discovered.device_name == device_name:
                self._on_import_update(service_name, discovered)
                return

    def _seed_http_url_from_cache(self, device_name: str) -> None:
        """Pull ``device_name``'s HTTP service URL out of zeroconf's cache.

        Handles the case where the HTTP service arrived first: the
        browser callback skipped storing the URL because no importable
        existed for that name yet. Now that one does, look directly at
        zeroconf's cache (no network round-trip) and stash the URL so
        the about-to-fire ``on_importable_added`` carries the right
        ``web_url``.
        """
        monitor = self._monitor
        if (zc := monitor._mdns.zeroconf) is None or monitor.state.http_urls.get(device_name):
            return
        info = AsyncServiceInfo(_HTTP_SERVICE_TYPE, f"{device_name}.{_HTTP_SERVICE_TYPE}")
        if not info.load_from_cache(zc.zeroconf):
            return
        monitor.state.http_urls[device_name] = _http_url_from_service_info(device_name, info)

    def _on_import_update(self, service_name: str, discovered: DiscoveredImport | None) -> None:
        """Bridge ``DashboardImportDiscovery`` → controller callbacks.

        ``service_name`` is the full mDNS service-instance name
        (``<device>._esphomelib._tcp.local.``); ``discovered`` is None
        on removal. We re-key by device name so callers don't have to
        carry the suffix, drop devices that are already configured
        locally (since the dashboard knows about them already), and
        translate the upstream ``DiscoveredImport`` shape into our
        ``AdoptableDevice`` model with the ``ignored`` flag filled in.
        """
        monitor = self._monitor
        device_name = device_name_from_service(service_name)
        if discovered is None:
            if monitor._on_importable_removed is not None:
                monitor._on_importable_removed(device_name)
            return
        if monitor._find_device_by_name(device_name) is not None:
            # Already configured — surfacing it as importable would
            # confuse the dashboard.
            return
        # Late-binding: if the HTTP service for this device is already
        # in zeroconf's cache (it arrived before the esphomelib
        # service), pull its URL now so the AdoptableDevice we emit
        # here carries it without waiting for the next HTTP re-announce.
        self._seed_http_url_from_cache(discovered.device_name)
        if monitor._on_importable_added is not None:
            monitor._on_importable_added(self._build_adoptable(discovered))

    def _build_adoptable(self, discovered: DiscoveredImport) -> AdoptableDevice:
        """Translate an upstream ``DiscoveredImport`` into our ``AdoptableDevice``.

        Single construction site for the cross-type mapping plus the
        two locally-known fields (``ignored`` from the persisted set,
        ``web_url`` from the HTTP-service cache). Used by both the
        live ADD path (``_on_import_update``) and the snapshot path
        (``get_importable_devices``) so the two views stay identical.
        """
        monitor = self._monitor
        return AdoptableDevice(
            name=discovered.device_name,
            friendly_name=discovered.friendly_name or "",
            package_import_url=discovered.package_import_url,
            project_name=discovered.project_name,
            project_version=discovered.project_version,
            network=discovered.network,
            ignored=monitor._is_ignored(discovered.device_name),
            web_url=monitor.state.http_urls.get(discovered.device_name, ""),
        )
