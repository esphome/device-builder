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

import asyncio
import ipaddress
import logging
import socket
from typing import TYPE_CHECKING

import ifaddr
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


def _is_loopback_adapter(adapter: ifaddr.Adapter) -> bool:
    """
    Return ``True`` when *adapter* is the host's loopback interface.

    Matches by interface name (``lo`` / ``lo0``) rather than by
    inspecting individual addresses: macOS configures ``fe80::1``
    on ``lo0``, which is a real link-local address as far as
    :mod:`ipaddress` is concerned (``is_loopback`` returns ``False``,
    ``is_link_local`` returns ``True``) but routes to nothing
    useful — advertising it would be misleading. Filtering the
    interface out wholesale catches every loopback IP in one
    place.
    """
    name = (adapter.name or "").lower()
    nice = (adapter.nice_name or "").lower()
    return name.startswith("lo") or "loopback" in nice


def _local_addresses() -> list[str]:
    """
    Return the IPv4 / IPv6 addresses to advertise.

    Enumerates every adapter via :mod:`ifaddr` (already a
    python-zeroconf dependency) and returns the bare addresses as
    plain strings suitable for :class:`~zeroconf.ServiceInfo`'s
    ``parsed_addresses`` keyword. Drops three classes of addresses
    that would land on the wire but never help a peer:

    * **Loopback interfaces.** Filtering by interface (``lo`` /
      ``lo0``) catches macOS's ``fe80::1``-on-``lo0`` link-local
      that wouldn't be caught by an ``ip.is_loopback`` check alone.
    * **Loopback IPs on non-loopback interfaces.** Defense in depth
      for hosts where the OS aliases ``127.0.0.1`` onto a real
      interface for some reason.
    * **IPv6 link-local addresses** (``fe80::/10``). Useless once
      the scope_id is dropped, which the mDNS wire format requires
      — a peer receiving a bare ``fe80::xxx`` has no way to know
      which interface to send the packet out on. Hosts with many
      virtual interfaces (VPN, awdl, utun*) carry a dozen link-
      local addresses that just inflate the announcement without
      adding reachability.

    Setting ``parsed_addresses`` explicitly is what fixes the
    "127.0.0.1 / ::1 / fe80::1 only" advertise we saw on macOS:
    when ``ServiceInfo`` is constructed with no addresses, peers
    fall back to A/AAAA lookups against the SRV target. On macOS
    that lookup is answered by ``mDNSResponder``, which can drop
    to loopback while the system's network state is in flux.
    Publishing the addresses ourselves takes that path out of the
    loop.

    .. note::

       :func:`ifaddr.get_adapters` does blocking I/O — reads
       ``/proc/net`` on Linux, calls ``GetAdaptersAddresses`` on
       Windows. Async callers must run this via
       :meth:`asyncio.AbstractEventLoop.run_in_executor` rather
       than calling it directly on the event loop. The
       :class:`DashboardAdvertiser`'s :meth:`~DashboardAdvertiser.register`
       method handles that for production use; tests that call this
       function synchronously off the loop don't need to.
    """
    out: list[str] = []
    for adapter in ifaddr.get_adapters():
        if _is_loopback_adapter(adapter):
            continue
        for ip in adapter.ips:
            # ``ifaddr.IP.ip`` is a ``str`` for IPv4 and a 3-tuple
            # ``(addr, flowinfo, scope_id)`` for IPv6. The ServiceInfo
            # wire format only carries the bare address — drop the
            # tuple framing.
            raw = ip.ip
            addr_str = raw[0] if isinstance(raw, tuple) else raw
            try:
                parsed = ipaddress.ip_address(addr_str)
            except ValueError:
                continue
            if parsed.is_loopback or parsed.is_link_local:
                continue
            out.append(addr_str)
    return out


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

    def build_service_info(self, addresses: list[str] | None = None) -> ServiceInfo:
        """
        Construct the ``ServiceInfo`` that will be published on register.

        *addresses* is the list of IP strings to publish in the A /
        AAAA records. ``None`` (the default) calls
        :func:`_local_addresses` synchronously, which is convenient
        for tests but does blocking I/O — :meth:`register` resolves
        the list via :meth:`asyncio.AbstractEventLoop.run_in_executor`
        and passes it in explicitly, keeping the event loop clean.

        Exposed (rather than inlined into :meth:`register`) so tests
        can introspect the payload without driving the full zeroconf
        register/unregister cycle.
        """
        if addresses is None:
            addresses = _local_addresses()
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
        # Publishing the host's addresses explicitly avoids relying
        # on the receiver's A/AAAA lookup against ``server``, which
        # on macOS can return loopback while mDNSResponder is in a
        # transient state. See ``_local_addresses``.
        return ServiceInfo(
            SERVICE_TYPE,
            instance,
            port=self._port,
            properties=properties,
            server=server,
            parsed_addresses=addresses,
        )

    async def register(self, zeroconf: AsyncZeroconf) -> None:
        """
        Publish the service via *zeroconf*.

        ``allow_name_change=True`` lets python-zeroconf disambiguate
        two dashboards on the same hostname (rare in practice, but
        the rename-on-conflict cost is one register call so the
        protection is essentially free).

        Address enumeration runs in the default executor:
        :func:`ifaddr.get_adapters` does blocking syscalls, which
        would trip blockbuster on Linux and stall the loop in
        production. The result is passed into
        :meth:`build_service_info` so the rest of the construction
        stays sync.
        """
        if self._info is not None:
            _LOGGER.debug("Dashboard advertise already registered; skipping")
            return
        loop = asyncio.get_running_loop()
        addresses = await loop.run_in_executor(None, _local_addresses)
        info = self.build_service_info(addresses)
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
