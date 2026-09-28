"""Poll for host address changes and reconcile zeroconf's sockets.

zeroconf binds its sockets once at construction and never notices interfaces
that appear or disappear afterward (a VPN coming up, Wi-Fi reconnecting, a
Docker network attaching). ``async_update_interfaces`` rescans and reconciles;
we drive it from a small ``ifaddr`` poll — the portable detection the zeroconf
docs recommend when no netlink / framework push-signal is wired.
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


def address_snapshot() -> frozenset[tuple[str, int]]:
    """Return the host's current (address, prefix) set; a change triggers a reconcile."""
    return frozenset(
        (_ip_to_str(ip.ip), ip.network_prefix)
        for adapter in ifaddr.get_adapters()
        for ip in adapter.ips
    )


class ZeroconfBinding(NamedTuple):
    """Interfaces and IP version the mDNS responder binds."""

    interfaces: list[str] | InterfaceChoice
    ip_version: IPVersion


def zeroconf_binding(pinned_ip_version: IPVersion | None = None) -> ZeroconfBinding:
    """
    Return the addresses and IP version the mDNS responder binds on this host.

    Non-loopback IPv4 and link-local IPv6 addresses; every interface when none
    qualify. Dual-stack never applies on darwin / freebsd.
    """
    v4: list[str] = []
    v6: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            address = _ip_to_str(ip.ip)
            if isinstance(ip.ip, tuple):
                if ip_address(address).is_link_local:
                    v6.append(address)
            elif is_usable_ip(address):
                v4.append(address)
    ip_version = pinned_ip_version or _select_ip_version(bool(v4), bool(v6))
    if ip_version is IPVersion.V4Only:
        v6 = []
    elif ip_version is IPVersion.V6Only:
        v4 = []
    return ZeroconfBinding(list(dict.fromkeys(v4 + v6)) or InterfaceChoice.All, ip_version)


def ipv4_only_binding(binding: ZeroconfBinding) -> ZeroconfBinding:
    """Return *binding* narrowed to its IPv4 addresses."""
    if isinstance(binding.interfaces, InterfaceChoice):
        return ZeroconfBinding(binding.interfaces, IPVersion.V4Only)
    v4 = [address for address in binding.interfaces if ":" not in address]
    return ZeroconfBinding(v4 or InterfaceChoice.All, IPVersion.V4Only)


async def async_zeroconf_binding() -> ZeroconfBinding:
    """Resolve ``zeroconf_binding`` off the event loop; every interface, IPv4 only, on failure."""
    try:
        return await run_in_executor(zeroconf_binding)
    except Exception:
        _LOGGER.exception("host address scan failed; mDNS responder stays IPv4 only")
        return ZeroconfBinding(InterfaceChoice.All, IPVersion.V4Only)


async def monitor_interfaces(
    zeroconf: AsyncEsphomeZeroconf,
    pinned_ip_version: IPVersion | None = None,
    interval: float = _INTERFACE_POLL_INTERVAL,
) -> None:
    """
    Reconcile zeroconf sockets whenever the host's addresses change, until cancelled.

    The IP version is re-selected on each change unless *pinned_ip_version* is set.
    """
    previous = await _safe_snapshot()
    while True:
        await asyncio.sleep(interval)
        current = await _safe_snapshot()
        # ``None`` is a failed snapshot, not "no addresses" — skip so a transient
        # ifaddr error can't be read as every interface disappearing.
        if current is None or current == previous:
            continue
        try:
            # A failed scan raises rather than resolving to ``V4Only``, so a
            # transient ifaddr error can't downgrade a dual-stack responder.
            binding = await run_in_executor(zeroconf_binding, pinned_ip_version)
            # A no-op when nothing the responder binds actually moved.
            await zeroconf.async_update_interfaces(
                interfaces=binding.interfaces, ip_version=binding.ip_version
            )
        except Exception:
            # Log and retry next tick; leave ``previous`` so the change re-attempts.
            _LOGGER.exception("zeroconf interface reconcile failed; will retry")
        else:
            _LOGGER.info(
                "Network interfaces changed; reconciled zeroconf sockets with %s",
                binding.ip_version,
            )
            previous = current


def _select_ip_version(has_v4: bool, has_v6: bool) -> IPVersion:  # noqa: FBT001
    if sys.platform.startswith(("darwin", "freebsd")):
        return IPVersion.V6Only if has_v6 and not has_v4 else IPVersion.V4Only
    return IPVersion.All if has_v6 else IPVersion.V4Only


def _ip_to_str(ip: str | tuple[str, int, int]) -> str:
    """Normalize an ``ifaddr`` IP (v4 string / v6 ``(addr, flowinfo, scope)`` tuple).

    Mirrors ``helpers.network_interfaces.resolve_bind_host``: keep the ``%scope``
    on link-local v6, drop flowinfo so a benign flowinfo change isn't read as
    churn (and so the snapshot is a stable string, not a tuple repr).
    """
    if isinstance(ip, str):
        return ip
    address, _flowinfo, scope_id = ip
    return f"{address}%{scope_id}" if scope_id else address


async def _safe_snapshot() -> frozenset[tuple[str, int]] | None:
    """Snapshot host addresses off the event loop; ``None`` on failure so the loop retries.

    ``ifaddr.get_adapters`` is blocking (reads /proc/net; GetAdaptersAddresses on
    Windows) and can raise on a transient OS hiccup; swallow it so one bad scan
    can't kill the reconciler for the rest of the process's life.
    """
    try:
        return await run_in_executor(address_snapshot)
    except Exception:
        _LOGGER.exception("host address snapshot failed; will retry")
        return None
