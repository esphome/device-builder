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

_LOGGER = logging.getLogger(__name__)

# Interface changes are rare, so a relaxed poll keeps steady-state wakeups low;
# 2 min still reflects a VPN/Wi-Fi/Docker change well before a user would
# investigate why a device isn't showing up.
_INTERFACE_POLL_INTERVAL = 120.0


def address_snapshot() -> frozenset[tuple[str, int]]:
    """Return the host's current (address, prefix) set; a change triggers a reconcile."""
    return frozenset(
        (str(ip.ip), ip.network_prefix) for adapter in ifaddr.get_adapters() for ip in adapter.ips
    )


async def monitor_interfaces(
    zeroconf: AsyncEsphomeZeroconf, interval: float = _INTERFACE_POLL_INTERVAL
) -> None:
    """Reconcile zeroconf sockets whenever the host's addresses change, until cancelled."""
    loop = asyncio.get_running_loop()
    # ``ifaddr.get_adapters`` is blocking (reads /proc/net; GetAdaptersAddresses
    # on Windows) — keep it off the WS event loop, per helpers/network_interfaces.
    previous = await loop.run_in_executor(None, address_snapshot)
    while True:
        await asyncio.sleep(interval)
        current = await loop.run_in_executor(None, address_snapshot)
        if current == previous:
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
