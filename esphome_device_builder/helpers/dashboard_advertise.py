"""
Publish the dashboard's own ``_esphomebuilder._tcp.local.`` service.

Phase 1 of the remote-build offload feature (issue #106). Dashboards
that browse this service type can list every other dashboard reachable
on the LAN — used by the eventual "Remote build" settings page on the
offloader and by the ESPHome Desktop welcome screen's "we found a
dashboard, want to connect?" detection.

The service-type label is ``_esphomebuilder`` rather than the
``_esphomedashboard`` named in the original design proposal: RFC
6335 §5.1 caps the label at 15 characters, ``esphomedashboard`` is
16, and ``esphomebuilder`` (14) is the closest project-identifying
alternative that fits. Parallels the existing ``_esphomelib._tcp.local.``
device service type so a packet capture shows both ESPHome surfaces
in the same ``_esphome*`` namespace.

The TXT record carries the two version fields a peer can't derive
from the browse response on its own:

* ``server_version`` — this dashboard's own package version, so a
  peer can flag a release-skew warning before pairing.
* ``esphome_version`` — the ``esphome`` library version this
  dashboard would compile against, so the version-mismatch warning
  in phase 7 can fire on the listing page rather than waiting for
  an upload to come back with a surprise build.

A friendly label and the host's mDNS name are *not* in TXT — both
are already on the wire. python-zeroconf exposes the service
instance name (the leftmost label of the published name, e.g.
``MacBook-Pro``) and the SRV record's target (the FQDN, e.g.
``MacBook-Pro.local.``) directly on the resolved ``ServiceInfo``;
duplicating them in TXT just bloats the packet.

The advertise reuses the existing ``AsyncEsphomeZeroconf`` instance
owned by :class:`~esphome_device_builder.controllers._device_state_monitor.DeviceStateMonitor`
so the dashboard ships one mDNS responder per process. When that
zeroconf failed to start (e.g. the port is held by avahi /
``mDNSResponder`` and we couldn't bind), the advertise is a no-op
rather than a hard failure — device discovery is the load-bearing
mDNS feature; the dashboard advertise is a nice-to-have.
"""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING

from zeroconf import ServiceInfo

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncZeroconf

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPE = "_esphomebuilder._tcp.local."


def _default_friendly_name() -> str:
    """
    Best-effort friendly label for the dashboard host.

    Uses the leftmost label of ``socket.gethostname()`` so a host
    that returns ``desktop.local`` advertises as ``desktop`` (the
    full FQDN goes in the ``hostname`` TXT field separately). Falls
    back to ``"esphome-dashboard"`` when the system can't report
    a hostname at all.
    """
    raw = socket.gethostname() or ""
    label = raw.split(".", 1)[0].strip()
    return label or "esphome-dashboard"


def _default_hostname() -> str:
    """
    System mDNS hostname for the ``hostname`` TXT field.

    Returns ``socket.gethostname()`` with ``.local`` appended when
    the result has no dot. Doesn't use ``socket.getfqdn()``: on
    macOS that resolver can return the reverse-DNS arpa form (e.g.
    ``...ip6.arpa``) when reverse lookup fails, which is worse
    than no hostname at all.
    """
    raw = (socket.gethostname() or "").strip()
    if not raw:
        return ""
    if "." in raw:
        return raw
    return f"{raw}.local"


class DashboardAdvertiser:
    """
    Publish the dashboard's ``_esphomedashboard._tcp.local.`` service.

    Constructed once per :class:`DeviceBuilder` lifetime. The
    :meth:`register` / :meth:`unregister` pair runs from the
    dashboard's start / stop hooks. Idempotent on both sides — calling
    ``register`` twice (or ``unregister`` without a prior register) is
    safe and logged at debug level.
    """

    def __init__(
        self,
        *,
        port: int,
        server_version: str,
        esphome_version: str,
        name: str | None = None,
        hostname: str | None = None,
    ) -> None:
        """
        Capture the static fields used in the published ``ServiceInfo``.

        ``port`` is the dashboard's HTTP listen port — what a peer
        connects to once it's chosen this advertisement from a
        browse. ``name`` defaults to the system hostname's leftmost
        label (used as both the friendly TXT field and the mDNS
        instance name); ``hostname`` defaults to the FQDN.
        """
        friendly = (name or "").strip() or _default_friendly_name()
        host = (hostname or "").strip() or _default_hostname()
        self._port = int(port)
        self._name = friendly
        self._hostname = host
        self._server_version = server_version
        self._esphome_version = esphome_version
        self._info: ServiceInfo | None = None
        self._zeroconf: AsyncZeroconf | None = None

    @property
    def service_type(self) -> str:
        """The mDNS service type this advertiser publishes under."""
        return SERVICE_TYPE

    @property
    def registered(self) -> bool:
        """True between a successful :meth:`register` and :meth:`unregister`."""
        return self._info is not None

    def build_service_info(self) -> ServiceInfo:
        """
        Construct the ``ServiceInfo`` that will be published on register.

        Exposed (rather than inlined into :meth:`register`) so tests
        can introspect the payload without driving the full zeroconf
        register/unregister cycle.
        """
        instance = f"{self._name}.{SERVICE_TYPE}"
        # TXT carries only what isn't already on the wire. The
        # service-instance label (``self._name``) and the SRV
        # target (``server`` below) are returned by every browse;
        # peers read them directly off ``ServiceInfo.name`` /
        # ``ServiceInfo.server`` rather than parsing TXT.
        properties = {
            "server_version": self._server_version,
            "esphome_version": self._esphome_version,
        }
        # ``server`` is the SRV record's target. Zeroconf appends
        # ``.local.`` if missing; pass the FQDN through as-is so a
        # host already advertising e.g. ``desktop.local`` keeps the
        # same answer it does for every other service.
        server = self._hostname if self._hostname.endswith(".") else f"{self._hostname}."
        return ServiceInfo(
            SERVICE_TYPE,
            instance,
            port=self._port,
            properties=properties,
            server=server,
        )

    async def register(self, zeroconf: AsyncZeroconf) -> None:
        """
        Publish the service via *zeroconf*.

        ``allow_name_change=True`` lets python-zeroconf disambiguate
        two dashboards on the same hostname (rare in practice, but
        the rename-on-conflict cost is one register call so the
        protection is essentially free).
        """
        if self._info is not None:
            _LOGGER.debug("Dashboard advertise already registered; skipping")
            return
        info = self.build_service_info()
        try:
            await zeroconf.async_register_service(info, allow_name_change=True)
        except Exception:
            _LOGGER.exception(
                "Failed to advertise dashboard on %s — peer discovery disabled",
                SERVICE_TYPE,
            )
            return
        self._info = info
        self._zeroconf = zeroconf
        _LOGGER.info(
            "Advertising dashboard on %s as %r (port %d, esphome %s)",
            SERVICE_TYPE,
            info.name,
            self._port,
            self._esphome_version,
        )

    async def unregister(self) -> None:
        """
        Withdraw the service.

        No-op when never registered or already unregistered. Failures
        are logged but not re-raised so dashboard shutdown stays clean
        even if the zeroconf socket is already gone.
        """
        info = self._info
        zeroconf = self._zeroconf
        self._info = None
        self._zeroconf = None
        if info is None or zeroconf is None:
            return
        try:
            await zeroconf.async_unregister_service(info)
        except Exception:
            _LOGGER.debug("Dashboard advertise unregister failed", exc_info=True)
