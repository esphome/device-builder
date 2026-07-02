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

import ifaddr
from esphome.zeroconf import AsyncEsphomeZeroconf

from ...helpers.async_ import run_in_executor

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


async def monitor_interfaces(
    zeroconf: AsyncEsphomeZeroconf, interval: float = _INTERFACE_POLL_INTERVAL
) -> None:
    """Reconcile zeroconf sockets whenever the host's addresses change, until cancelled."""
    previous = await _safe_snapshot()
    while True:
        await asyncio.sleep(interval)
        current = await _safe_snapshot()
        # ``None`` is a failed snapshot, not "no addresses" — skip so a transient
        # ifaddr error can't be read as every interface disappearing.
        if current is None or current == previous:
            continue
        try:
            # No-arg reuses the construction-time ``InterfaceChoice.All``, so this
            # rescans every interface; a no-op when nothing actually moved.
            await zeroconf.async_update_interfaces()
        except Exception:
            # Log and retry next tick; leave ``previous`` so the change re-attempts.
            _LOGGER.exception("zeroconf interface reconcile failed; will retry")
        else:
            _LOGGER.info("Network interfaces changed; reconciled zeroconf sockets")
            previous = current


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
