"""On-demand connectivity probe behind the offline troubleshooting dialog."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import DeviceTroubleshootResult, ErrorCode
from .._device_state_monitor import shared
from .._device_state_monitor.helpers import _pick_ipv4

if TYPE_CHECKING:
    from ...models import Device
    from .._device_state_monitor.controller import DeviceStateMonitor
    from .controller import DevicesController

# Worst-case DNS resolve (3s try + 3s ``.local`` fallback) is cut at 4s
# and the retrying ping at 5s so the whole probe answers inside the
# frontend's 10s command timeout.
_DNS_TIMEOUT = 4.0
_PING_TIMEOUT = 5.0


async def run(controller: DevicesController, device_name: str) -> DeviceTroubleshootResult:
    """Probe DNS, mDNS, and ICMP for *device_name* and report the evidence."""
    bucket = controller._scanner.get_by_name(device_name)
    if not bucket:
        raise CommandError(ErrorCode.NOT_FOUND, f"No configured device named {device_name!r}")
    device = bucket[0]
    monitor = controller._state_monitor
    mdns = monitor.mdns
    address = device.address
    result = DeviceTroubleshootResult(
        device=device_name,
        address=address,
        icmp_available=monitor.ping.icmp_available,
        zeroconf_running=mdns.zeroconf is not None,
        dns_had_cached_failure=bool(address)
        and monitor.state.dns_cache.has_cached_failure(address),
    )
    dns_addresses, _ = await asyncio.gather(
        _resolve_dns(monitor, address), mdns.refresh_mdns(device_name)
    )
    if dns_addresses:
        result.dns_resolved = True
        result.dns_addresses = dns_addresses
    result.mdns_addresses = mdns.get_cached_addresses(f"{device_name}.local") or []
    result.mdns_has_cached_trace = mdns.has_cached_trace(device_name)
    result.mdns_has_live_anchor_ptr = mdns.has_live_anchor_ptr(device_name)
    target = _pick_target(device, dns_addresses, result.mdns_addresses)
    if result.icmp_available and target:
        result.ping_attempted = True
        result.ping_target = target
        result.ping_rtt_ms = await _ping(monitor, device_name, target)
    return result


async def _resolve_dns(monitor: DeviceStateMonitor, address: str) -> list[str] | None:
    if not address:
        return None
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_DNS_TIMEOUT):
            return await monitor.state.dns_cache.async_resolve(address)
    return None


def _pick_target(device: Device, dns_addresses: list[str] | None, mdns_addresses: list[str]) -> str:
    # The sweep's resolution chain, plus the persisted last-known IP so
    # a dynamic-IP diagnosis still has something to probe.
    addresses = dns_addresses or mdns_addresses or list(device.runtime_state.ip_addresses)
    if not addresses and device.ip:
        addresses = [device.ip]
    return _pick_ipv4(addresses) if addresses else ""


async def _ping(monitor: DeviceStateMonitor, name: str, target: str) -> float | None:
    rtt: float | None = None
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_PING_TIMEOUT):
            async with monitor.ping.icmp_concurrency:
                rtt = await monitor.ping.ping_once(target, retry=True)
    # A hit heals state through the normal ping source; a miss applies
    # the same OFFLINE verdict the sweep would.
    shared.apply_ping_result(monitor, name, rtt)
    return rtt
