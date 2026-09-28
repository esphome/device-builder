"""Poll for host address changes and reconcile zeroconf's sockets.

zeroconf binds its sockets once at construction and never notices interfaces
that appear or disappear afterward (a VPN coming up, Wi-Fi reconnecting, a
Docker network attaching). ``async_update_interfaces`` rescans and reconciles;
we drive it from a small ``ifaddr`` poll — the portable detection the zeroconf
docs recommend when no netlink / framework push-signal is wired. The same scan
selects the addresses and IP version the responder binds.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from ipaddress import ip_address
from typing import NamedTuple

import ifaddr
from esphome.zeroconf import AsyncEsphomeZeroconf
from zeroconf import InterfaceChoice, IPVersion

from ...helpers.async_ import run_in_executor
from ...helpers.ip import is_usable_ip

_LOGGER = logging.getLogger(__name__)

# Interface changes are rare, so a relaxed poll keeps steady-state wakeups low.
# Matches DashboardAdvertiser's _REFRESH_INTERVAL_SECONDS (the existing adapter
# poll) so the two share one cadence; a change still reconciles well before a
# user would investigate why a device isn't showing up.
_INTERFACE_POLL_INTERVAL = 300.0


# zeroconf skips, without raising, a link-local IPv6 address that is still in
# duplicate address detection; one follow-up reconcile shortly after each bind
# picks it up.
_SETTLE_DELAY = 10.0


class ZeroconfBinding(NamedTuple):
    """Interfaces and IP version the mDNS responder binds."""

    interfaces: list[str] | InterfaceChoice
    ip_version: IPVersion


FALLBACK_BINDING = ZeroconfBinding(InterfaceChoice.All, IPVersion.V4Only)


class HostAddresses(NamedTuple):
    """The host's non-loopback IPv4 and link-local IPv6 addresses."""

    v4: tuple[str, ...]
    v6: tuple[str, ...]

    def binding(self, pinned_ip_version: IPVersion | None = None) -> ZeroconfBinding:
        """
        Return the responder binding, on every interface when no address qualifies.

        Dual-stack needs a link-local IPv6 address and never applies on darwin / freebsd.
        """
        ip_version = pinned_ip_version or _select_ip_version(bool(self.v4), bool(self.v6))
        interfaces = [
            *(self.v4 if ip_version is not IPVersion.V6Only else ()),
            *(self.v6 if ip_version is not IPVersion.V4Only else ()),
        ]
        return ZeroconfBinding(interfaces or InterfaceChoice.All, ip_version)


def scan_host() -> HostAddresses:
    """Return the addresses the mDNS responder can bind on this host."""
    v4: dict[str, None] = {}
    v6: dict[str, None] = {}
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            address = _ip_to_str(ip.ip)
            if isinstance(ip.ip, tuple):
                if ip_address(address).is_link_local:
                    v6[address] = None
            elif is_usable_ip(address):
                v4[address] = None
    return HostAddresses(tuple(v4), tuple(v6))


def startup_bindings(addresses: HostAddresses | None) -> list[ZeroconfBinding]:
    """Return the bindings to try at startup: the selected one, then its IPv4-only fallback."""
    if addresses is None:
        return [FALLBACK_BINDING]
    selected = addresses.binding()
    if selected.ip_version is IPVersion.V4Only:
        return [selected]
    return [selected, addresses.binding(IPVersion.V4Only)]


async def async_scan_host() -> HostAddresses | None:
    """Scan the host off the event loop; ``None`` on failure."""
    try:
        return await run_in_executor(scan_host)
    except Exception:
        _LOGGER.exception("host address scan failed; will retry")
        return None


async def monitor_interfaces(
    zeroconf: AsyncEsphomeZeroconf,
    applied: ZeroconfBinding | None = None,
    pinned_ip_version: IPVersion | None = None,
    interval: float = _INTERFACE_POLL_INTERVAL,
) -> None:
    """
    Reconcile zeroconf sockets whenever the host's binding changes, until cancelled.

    *applied* is the binding the responder holds. One extra reconcile runs
    ``_SETTLE_DELAY`` after startup and after each change.
    """
    settle = True
    settle_delay = min(interval, _SETTLE_DELAY)
    delay = settle_delay
    while True:
        await asyncio.sleep(delay)
        # A failed scan or reconcile retries at the poll interval, not the settle delay.
        delay = interval
        addresses = await async_scan_host()
        # ``None`` is a failed scan, not "no addresses" — skip so a transient
        # ifaddr error can't be read as every interface disappearing.
        if addresses is None:
            continue
        binding = addresses.binding(pinned_ip_version)
        changed = binding != applied
        if not changed and not settle:
            continue
        try:
            # A no-op when every address in the binding already has its socket.
            await zeroconf.async_update_interfaces(
                interfaces=binding.interfaces, ip_version=binding.ip_version
            )
        except Exception:
            # Log and retry next tick; leave ``applied`` so the change re-attempts.
            _LOGGER.exception("zeroconf interface reconcile failed; will retry")
            continue
        if changed:
            _LOGGER.info(
                "Network interfaces changed; reconciled zeroconf sockets with %s on %s",
                binding.ip_version,
                binding.interfaces,
            )
        applied = binding
        settle = changed
        if settle:
            delay = settle_delay


def _select_ip_version(has_v4: bool, has_v6: bool) -> IPVersion:  # noqa: FBT001
    if sys.platform.startswith(("darwin", "freebsd")):
        return IPVersion.V6Only if has_v6 and not has_v4 else IPVersion.V4Only
    return IPVersion.All if has_v6 else IPVersion.V4Only


def _ip_to_str(ip: str | tuple[str, int, int]) -> str:
    """Normalize an ``ifaddr`` IP (v4 string / v6 ``(addr, flowinfo, scope)`` tuple).

    Mirrors ``helpers.network_interfaces.resolve_bind_host``: keep the ``%scope``
    on link-local v6, drop flowinfo so a benign flowinfo change isn't read as
    churn (and so the address is a stable string, not a tuple repr).
    """
    if isinstance(ip, str):
        return ip
    address, _flowinfo, scope_id = ip
    return f"{address}%{scope_id}" if scope_id else address
