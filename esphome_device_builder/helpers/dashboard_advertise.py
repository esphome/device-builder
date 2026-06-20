"""
Publish the dashboard's own ``_esphomebuilder._tcp.local.`` service.

Part of the remote-build offload feature (issue #106). Dashboards
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

The TXT record carries the fields a peer can't derive from the
browse response on its own:

* ``server_version`` — this dashboard's own package version, so a
  peer can flag a release-skew warning before pairing.
* ``esphome_version`` — the ``esphome`` library version this
  dashboard would compile against, so the version-mismatch warning
  can fire on the listing page rather than waiting for an upload
  to come back with a surprise build.
* ``pin_sha256`` (optional) — SHA-256 of the receiver's X25519
  peer-link public key (lowercase hex). Peers cross-check the
  responder static key observed during the Noise XX handshake
  against this TXT entry; the fingerprint is also what pairing
  pins out-of-band. Omitted when the identity helper hasn't run
  yet.
* ``remote_build_port`` (optional) — the plain-TCP port the
  receiver's peer-link Noise WS listener is bound to. Carried in
  TXT so paired peers connect to the right port even when the
  operator has overridden ``--remote-build-port``. Omitted when
  the receiver site isn't bound (default-off mode).
* ``friendly_name`` (optional) — the human machine label (e.g.
  ``MacBook-Pro``), since the instance name / SRV target are
  opaque per-install identifiers (``esphome-builder-<id>``).

The host's mDNS name is *not* in TXT — it's already on the wire.
python-zeroconf exposes the SRV record's target directly on the
resolved ``ServiceInfo``; duplicating it in TXT just bloats the packet.

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
import re
import socket
from typing import TYPE_CHECKING

import ifaddr
from zeroconf import ServiceInfo

if TYPE_CHECKING:
    from esphome.zeroconf import AsyncEsphomeZeroconf

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPE = "_esphomebuilder._tcp.local."

# Cadence at which the advertiser polls ``_local_addresses`` for
# changes and re-publishes via ``async_update_service`` if the set
# differs from what's currently on the wire. Five minutes balances
# "DHCP renewal / WiFi reconnect should be picked up before a peer's
# pairing breaks for too long" against "don't burn CPU walking
# adapters every minute". Refresh is a no-op when the address set
# hasn't changed (see :meth:`DashboardAdvertiser.refresh`), so the
# steady-state cost is one ``ifaddr.get_adapters`` call per tick
# with zero wire traffic.
_REFRESH_INTERVAL_SECONDS = 300


def _default_friendly_name() -> str:
    """
    Best-effort friendly label for the dashboard host.

    Uses the leftmost label of ``socket.gethostname()`` so a host
    that returns ``desktop.local`` advertises as ``desktop`` (this
    label is what becomes the mDNS service-instance name, i.e. the
    bit before ``._esphomebuilder._tcp.local.``; the FQDN is
    carried separately as the ``ServiceInfo.server`` SRV target).
    Falls back to ``"esphome-dashboard"`` when the system can't
    report a hostname at all.
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


# Interface-name prefixes for virtualisation / container bridges.
# Their IPs are host-namespace-scoped; advertising e.g. ``docker0``
# at ``172.17.0.1`` directs peers with the same Docker default
# subnet at their own bridge gateway. The HA addon runs with host
# networking, so the container also sees the Supervisor ``hassio``
# bridge (``172.30.32.0/23``); it is host-internal like ``docker0``
# and must not reach the wire.
_VIRTUAL_BRIDGE_PREFIXES: tuple[str, ...] = (
    "docker",  # docker0, docker_gwbridge
    "hassio",  # HA Supervisor internal bridge (172.30.32.0/23)
    "veth",  # virtual ethernet pair peer
    "cni",  # Kubernetes CNI plugin bridges
    "virbr",  # libvirt default bridges
    "vethernet",  # Windows Hyper-V virtual switches
)

# Docker user-defined networks: ``br-`` plus a 12-hex-char network
# ID. Anchored so real LAN bridge names (``br-lan``, ``br-guest``
# on OpenWRT / homelab Linux setups) stay out of the filter.
_DOCKER_USER_BRIDGE_RE = re.compile(r"^br-[0-9a-f]{12}$")


def _is_virtual_bridge_adapter(adapter: ifaddr.Adapter) -> bool:
    """Match a virtual-bridge prefix or Docker's ``br-<12 hex>`` user-network name."""
    name = (adapter.name or "").lower()
    nice = (adapter.nice_name or "").lower()
    if _DOCKER_USER_BRIDGE_RE.match(name) or _DOCKER_USER_BRIDGE_RE.match(nice):
        return True
    return any(name.startswith(p) or nice.startswith(p) for p in _VIRTUAL_BRIDGE_PREFIXES)


def _local_addresses() -> list[str]:
    """
    Return the IPv4 / IPv6 addresses to advertise.

    Enumerates every adapter via :mod:`ifaddr` (already a
    python-zeroconf dependency) and returns the bare addresses as
    plain strings suitable for :class:`~zeroconf.ServiceInfo`'s
    ``parsed_addresses`` keyword. Drops four classes of addresses
    that would land on the wire but never help a peer:

    * **Loopback interfaces.** Filtering by interface (``lo`` /
      ``lo0``) catches macOS's ``fe80::1``-on-``lo0`` link-local
      that wouldn't be caught by an ``ip.is_loopback`` check alone.
    * **Loopback IPs on non-loopback interfaces.** Defense in depth
      for hosts where the OS aliases ``127.0.0.1`` onto a real
      interface for some reason.
    * **Link-local addresses** — both IPv6 (``fe80::/10``) and
      IPv4 (``169.254.0.0/16``). IPv6 link-local is useless once
      the scope_id is dropped (which the mDNS wire format
      requires) — a peer receiving a bare ``fe80::xxx`` has no way
      to know which interface to send the packet out on. IPv4
      link-local (APIPA) only appears when DHCP has failed; a
      dashboard advertising itself on ``169.254.x.x`` would just
      attract pairings that immediately break the next time DHCP
      comes back. Hosts with many virtual interfaces (VPN, awdl,
      utun*) can carry a dozen link-local addresses that just
      inflate the announcement without adding reachability.
    * **Virtualisation / container bridges** — ``docker*``,
      ``veth*``, ``cni*``, ``virbr*``, ``vEthernet*`` prefixes
      plus Docker's ``br-<12 hex>`` user-defined networks (real
      LAN bridges like ``br-lan`` keep their IP). Host-namespace-
      scoped; advertising ``172.17.0.1`` from ``docker0`` points
      peers with the same default Docker subnet at their own
      bridge gateway. See ``_VIRTUAL_BRIDGE_PREFIXES`` +
      ``_DOCKER_USER_BRIDGE_RE``.

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
    seen: set[str] = set()
    out: list[str] = []
    for adapter in ifaddr.get_adapters():
        if _is_loopback_adapter(adapter) or _is_virtual_bridge_adapter(adapter):
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
            # De-duplicate while preserving discovery order: an IP
            # bound to multiple adapters (e.g. a primary + an alias
            # on the same NIC) would otherwise appear twice in the
            # advertise and trigger spurious ``refresh`` updates if
            # the duplicate flickers in/out between enumerations.
            if addr_str in seen:
                continue
            seen.add(addr_str)
            out.append(addr_str)
    return out


_DASHBOARD_ID_SUFFIX_CHARS = 8
_STABLE_HOSTNAME_PREFIX = "esphome-builder"


def build_mdns_hostname(*, dashboard_id: str = "") -> str:
    """
    Stable mDNS SRV target derived only from the persisted *dashboard_id*.

    Returns ``esphome-builder-{short_dashboard_id}.local`` — a fixed
    product prefix plus the first :data:`_DASHBOARD_ID_SUFFIX_CHARS`
    sanitised (RFC 1123) chars of *dashboard_id*. Never reads the OS
    hostname, so the target can't flip across reboots. Falls back to
    ``esphome-builder.local`` when *dashboard_id* is empty (transient
    pre-identity shape only).
    """
    suffix = _sanitize_dns_label(dashboard_id)[:_DASHBOARD_ID_SUFFIX_CHARS].strip("-")
    if suffix:
        return f"{_STABLE_HOSTNAME_PREFIX}-{suffix}.local"
    return f"{_STABLE_HOSTNAME_PREFIX}.local"


_DNS_LABEL_DISALLOWED_RE = re.compile(r"[^a-z0-9_-]")


def _sanitize_dns_label(raw: str) -> str:
    """Strip *raw* to an RFC 1123 hostname label (``[a-z0-9-]``).

    Lowercases, drops anything outside ``[a-z0-9_-]``, then maps
    the surviving underscores to hyphens (the base64url alphabet
    used by :func:`secrets.token_urlsafe` carries ``_``;
    python-zeroconf won't publish it in a hostname label).
    Anchoring the character class to ASCII ``a-z`` (not the
    Unicode-aware regex word class or :meth:`str.isalnum`) keeps
    non-ASCII letters out of the wire shape: a strict RFC 1123
    label can't carry them, and a host whose ``socket.gethostname()``
    returns e.g. ``café`` would otherwise produce a label
    python-zeroconf refuses to publish.

    Caller is responsible for trimming the result to the 63-octet
    per-label cap (RFC 1035 §2.3.4) and stripping any boundary
    hyphens left by truncation.
    """
    return _DNS_LABEL_DISALLOWED_RE.sub("", raw.strip().lower()).replace("_", "-")


class DashboardAdvertiser:
    """
    Publish the dashboard's ``_esphomebuilder._tcp.local.`` service.

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
        pin_sha256: str | None = None,
        remote_build_port: int | None = None,
        name: str | None = None,
        hostname: str | None = None,
        dashboard_id: str | None = None,
    ) -> None:
        """
        Capture the static fields used in the published ``ServiceInfo``.

        ``port`` is the dashboard's HTTP listen port. ``name`` defaults
        to the system hostname's leftmost label and becomes the
        ``friendly_name`` TXT entry (not lowercased — operators name
        machines with intentional case).

        ``hostname`` lands in the SRV record's target. With
        ``dashboard_id`` (production) it is composed as
        ``esphome-builder-{short_dashboard_id}.local`` via
        :func:`build_mdns_hostname`; an explicit ``hostname`` overrides
        that (tests). The SRV target is not duplicated in TXT — peers
        read it off ``ServiceInfo.server``.

        ``pin_sha256`` is SHA-256 of the receiver's X25519 peer-link
        public key (lowercase hex). When set, peers who browse the
        broadcast can sanity-check the responder static key observed
        during the Noise XX handshake against this TXT entry — a
        useful tampering tripwire on top of the out-of-band-confirmed
        pin from pairing. ``None`` when the identity helper hasn't
        run yet, or when the dashboard's own remote-build feature is
        disabled.

        ``remote_build_port`` is the plain-TCP port the receiver's
        peer-link Noise WS listener is bound to. Carried in TXT
        so paired peers can connect to the right port without
        re-typing it; the SRV record's port stays at the dashboard's
        main HTTP port (``port`` arg) so the existing browse path
        for general dashboard discovery isn't broken. ``None`` when
        the listener isn't bound (default-off shape).
        """
        friendly = (name or "").strip() or _default_friendly_name()
        explicit_host = (hostname or "").strip()
        host = explicit_host or build_mdns_hostname(dashboard_id=dashboard_id or "")
        self._port = int(port)
        # Human label rides in TXT; the instance label is the stable
        # SRV target's leftmost label.
        self._friendly_name = friendly
        self._name = host.split(".", 1)[0]
        self._hostname = host
        self._server_version = server_version
        self._esphome_version = esphome_version
        self._pin_sha256 = pin_sha256
        self._remote_build_port = remote_build_port
        self._info: ServiceInfo | None = None
        self._zeroconf: AsyncEsphomeZeroconf | None = None
        # Background tick that calls :meth:`refresh` on
        # ``_REFRESH_INTERVAL_SECONDS`` so DHCP renewals / WiFi
        # reconnects pick up new addresses without a dashboard
        # restart. Started in :meth:`register`, cancelled in
        # :meth:`unregister`.
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def service_type(self) -> str:
        """The mDNS service type this advertiser publishes under."""
        return SERVICE_TYPE

    @property
    def registered(self) -> bool:
        """True between a successful :meth:`register` and :meth:`unregister`."""
        return self._info is not None

    def set_pin_sha256(self, pin_sha256: str | None) -> None:
        """
        Update the published cert pin and refresh the broadcast.

        Called when the remote-build receiver site comes up and
        the cert + key have been loaded; lets the advertiser
        carry ``pin_sha256`` in TXT without having to know the
        identity helper at construction time. A subsequent
        :meth:`refresh` (the periodic background tick already
        does this) re-publishes the ServiceInfo with the new
        property. Safe to call before / after :meth:`register`;
        if not yet registered, the value is simply captured for
        the next ``build_service_info`` call.
        """
        self._pin_sha256 = pin_sha256

    def set_remote_build_port(self, remote_build_port: int | None) -> None:
        """
        Update the published remote-build listener port.

        Same shape as :meth:`set_pin_sha256` — captured here, picked
        up by the next ``build_service_info`` (the periodic refresh
        re-publishes). Lets paired peers find the listener port
        without having to re-type it after a ``--remote-build-port``
        override.
        """
        self._remote_build_port = remote_build_port

    @property
    def service_instance_name(self) -> str | None:
        """
        The published mDNS service-instance name, or ``None``.

        Returns the fully-qualified instance name as zeroconf
        registered it (e.g. ``esphome-builder-jwywnve._esphomebuilder._tcp.local.``,
        or ``esphome-builder-jwywnve-2._esphomebuilder._tcp.local.`` after a
        ``allow_name_change`` collision rename). ``None`` when the
        advertiser hasn't registered yet — same shape callers
        already use to gate other operations on ``registered``.

        Public surface so peer-discovery code can filter our own
        broadcast out of its discovered list without reaching into
        the private :attr:`_info`.
        """
        return self._info.name if self._info is not None else None

    @property
    def service_target_endpoint(self) -> tuple[str, int] | None:
        """
        The published ``(server, port)`` SRV target, or ``None``.

        ``server`` is the SRV record's target (the FQDN peers
        will connect to, normalised to lowercase with the trailing
        dot stripped); ``port`` is the dashboard's HTTP listen
        port. Returned as a tuple so peer-discovery code can do
        a single equality check against the resolved endpoint
        of every browsed service to filter our own broadcast on
        the (host, port) axis rather than the service-instance
        name axis (which can drift if zeroconf rename-on-conflict
        kicks in between our own register and our own browse
        callback).

        Two dashboards on the same host with different ports are
        legitimate distinct peers; matching on both axes preserves
        that ability. ``None`` when the advertiser hasn't
        registered yet, same shape as :attr:`service_instance_name`.
        """
        if self._info is None:
            return None
        server = self._info.server
        port = self._info.port
        if server is None or port is None:
            return None
        return (server.rstrip(".").lower(), port)

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
        # The SRV target (``server`` below) is returned by every browse,
        # so it isn't duplicated in TXT. ``friendly_name`` is the human
        # label peers display (the instance name is opaque).
        properties: dict[str, str] = {
            "server_version": self._server_version,
            "esphome_version": self._esphome_version,
        }
        if self._friendly_name:
            properties["friendly_name"] = self._friendly_name
        if self._pin_sha256:
            properties["pin_sha256"] = self._pin_sha256
        if self._remote_build_port is not None:
            properties["remote_build_port"] = str(self._remote_build_port)
        # ``server`` is the SRV record's target. Zeroconf appends
        # ``.local.`` if missing; pass it through as-is.
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

    async def register(self, zeroconf: AsyncEsphomeZeroconf) -> None:
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
            "Advertising dashboard on %s as %r (%r, port %d, esphome %s)",
            SERVICE_TYPE,
            info.name,
            self._friendly_name,
            self._port,
            self._esphome_version,
        )
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="dashboard-advertise-refresh"
        )

    async def _refresh_loop(self) -> None:
        """
        Background task that polls :meth:`refresh` on a fixed cadence.

        Sleeps ``_REFRESH_INTERVAL_SECONDS`` between checks. Exits
        cleanly on cancellation (the ``CancelledError`` raised by
        :func:`asyncio.sleep` propagates out of the loop and the
        task finishes) so :meth:`unregister` can drain it without
        special handling.

        Refresh exceptions are caught and logged at debug level —
        a transient zeroconf glitch shouldn't kill the whole
        refresh loop and leave the advertise stuck on stale
        addresses until the dashboard restarts. The next tick
        retries.
        """
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)
            try:
                await self.refresh()
            except Exception:
                _LOGGER.debug(
                    "Dashboard advertise refresh tick raised; will retry next interval",
                    exc_info=True,
                )

    async def refresh(self) -> bool:
        """
        Re-publish the advertise if anything observable on the wire changed.

        Compares both the local-address set AND the TXT properties
        against what's currently published; calls
        :meth:`AsyncEsphomeZeroconf.async_update_service` only when
        either differs. The no-op return path keeps callers free to
        invoke this on a tick / interface-change event / TXT-field
        update without flooding the network with unchanged updates.

        Returns ``True`` if a re-publish actually fired, ``False``
        when the cached state matched (or when the advertiser isn't
        currently registered, in which case there's nothing to
        refresh against).
        """
        info = self._info
        zeroconf = self._zeroconf
        if info is None or zeroconf is None:
            return False
        loop = asyncio.get_running_loop()
        new_addresses = await loop.run_in_executor(None, _local_addresses)
        new_info = self.build_service_info(new_addresses)
        # Preserve the instance name zeroconf actually registered.
        # ``async_register_service(allow_name_change=True)`` renames the
        # ServiceInfo in place on a collision (``green`` → ``green-2``),
        # but ``build_service_info`` always recomposes from ``self._name``.
        # Without this, ``async_update_service`` would announce the
        # pre-collision name — a second, conflicting record rather than an
        # update — and ``service_instance_name`` would report the wrong name.
        new_info.name = info.name
        # Compare normalized sets so the order ifaddr returns
        # interfaces in (which can shift between calls on some
        # platforms) doesn't trigger a spurious re-publish. Also
        # compare TXT properties so a setter-driven change (e.g.
        # ``set_pin_sha256``, ``set_remote_build_port``) actually
        # makes it onto the wire — without this, a TXT update
        # after register would never propagate.
        addresses_unchanged = sorted(new_addresses) == sorted(info.parsed_addresses())
        properties_unchanged = new_info.properties == info.properties
        if addresses_unchanged and properties_unchanged:
            return False
        try:
            await zeroconf.async_update_service(new_info)
        except Exception:
            _LOGGER.debug("Dashboard advertise refresh failed", exc_info=True)
            return False
        self._info = new_info
        _LOGGER.debug(
            "Refreshed dashboard advertise — addresses changed (%d → %d)",
            len(info.parsed_addresses()),
            len(new_addresses),
        )
        return True

    async def unregister(self) -> None:
        """
        Withdraw the service.

        No-op when never registered or already unregistered. Failures
        are logged but not re-raised so dashboard shutdown stays clean
        even if the zeroconf socket is already gone.
        """
        info = self._info
        zeroconf = self._zeroconf
        refresh_task = self._refresh_task
        self._info = None
        self._zeroconf = None
        self._refresh_task = None
        # Cancel the periodic refresh first so a tick already in
        # flight can't race the ``async_unregister_service`` call
        # below (refresh's ``async_update_service`` after we tore
        # down would either fail or race with the unregister).
        # Always drain — even an already-``done`` task may have
        # ended with an exception we want to surface to the
        # debug log instead of dropping silently.
        if refresh_task is not None:
            if not refresh_task.done():
                refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.debug("Dashboard advertise refresh task drain failed", exc_info=True)
        if info is None or zeroconf is None:
            return
        try:
            await zeroconf.async_unregister_service(info)
        except Exception:
            _LOGGER.debug("Dashboard advertise unregister failed", exc_info=True)
