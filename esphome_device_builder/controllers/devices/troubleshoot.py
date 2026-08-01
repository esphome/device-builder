"""On-demand connectivity probe behind the offline troubleshooting dialog."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import DeviceState, DeviceTroubleshootResult, ErrorCode
from .._device_state_monitor import shared
from .._device_state_monitor.helpers import _pick_ipv4

if TYPE_CHECKING:
    from ...models import Device
    from .._device_state_monitor.controller import DeviceStateMonitor
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def run(controller: DevicesController, configuration: str) -> DeviceTroubleshootResult:
    """
    Probe DNS, mDNS, and ICMP for *configuration*'s device and report the evidence.

    No probe budget of its own: every leg self-bounds (the resolver and
    mDNS re-query carry internal timeouts, icmplib bounds each packet)
    and a sweep-contended ICMP slot is waited for honestly. The
    frontend's command timeout is the backstop.
    """
    device = controller.get_by_configuration(configuration)
    if device is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"No configuration named {configuration!r}")
    monitor = controller._state_monitor
    mdns = monitor.mdns
    address = device.address
    result = DeviceTroubleshootResult(
        configuration=configuration,
        address=address,
        icmp_available=monitor.ping.icmp_available,
        zeroconf_running=mdns.zeroconf is not None,
        dns_had_cached_failure=bool(address)
        and monitor.state.dns_cache.has_cached_failure(address),
    )
    if address:
        # Capture the sweep's cached verdict above, then drop the entry
        # so "Check again" re-resolves on the wire instead of replaying
        # a cached failure for the rest of its TTL.
        monitor.state.dns_cache.invalidate(address)
    dns_result, mdns_result = await asyncio.gather(
        monitor.state.dns_cache.async_resolve(address) if address else _none(),
        mdns.refresh_mdns(device.name),
        return_exceptions=True,
    )
    if isinstance(mdns_result, BaseException):
        _LOGGER.warning(
            "mDNS re-query failed for %s; continuing", device.name, exc_info=mdns_result
        )
    dns_addresses: list[str] | None = None
    if isinstance(dns_result, BaseException):
        _LOGGER.warning("DNS resolve failed for %s; continuing", address, exc_info=dns_result)
    else:
        dns_addresses = dns_result
    if dns_addresses:
        result.dns_resolved = True
        result.dns_addresses = dns_addresses
        # The sweep records what it resolves before pinging; mirror it so
        # a fresh dynamic-IP resolve reaches ``device.ip`` immediately.
        monitor.apply_ip_addresses(device.name, dns_addresses)
    result.mdns_addresses = mdns.get_cached_addresses(f"{device.name}.local") or []
    result.mdns_has_cached_trace = mdns.has_cached_trace(device.name)
    result.mdns_has_live_anchor_ptr = mdns.has_live_anchor_ptr(device.name)
    target = _pick_target(device, dns_addresses, result.mdns_addresses)
    if result.icmp_available and target:
        result.ping_attempted = True
        result.ping_target = target
        result.ping_rtt_ms = await _ping(monitor, device, target)
    return result


async def _none() -> None:
    return None


def _pick_target(device: Device, dns_addresses: list[str] | None, mdns_addresses: list[str]) -> str:
    # The sweep's resolution chain, plus the persisted last-known IP so
    # a dynamic-IP diagnosis still has something to probe.
    addresses = dns_addresses or mdns_addresses or list(device.runtime_state.ip_addresses)
    if not addresses and device.ip:
        addresses = [device.ip]
    return _pick_ipv4(addresses) if addresses else ""


async def _ping(monitor: DeviceStateMonitor, device: Device, target: str) -> float | None:
    """ICMP *target* under the shared budget and apply the verdict via the ping source."""
    async with monitor.ping.icmp_concurrency:
        # Sweep-mirrored retry policy: an already-OFFLINE device gets a
        # clean single-packet verdict instead of a slow retry cycle.
        retry = device.runtime_state.state is not DeviceState.OFFLINE
        rtt = await monitor.ping.ping_once(target, retry=retry)
    shared.apply_ping_result(monitor, device.name, rtt)
    return rtt
