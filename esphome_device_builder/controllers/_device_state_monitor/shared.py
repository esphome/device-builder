"""Cross-cutting helpers shared by the mDNS browser path and the ping source.

Each free function takes the monitor as its first argument; the
monitor reaches sibling sources through ``state`` and through
``_mdns`` / ``_ping`` / ``_importable`` attributes.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ...helpers.hostname import is_local_hostname
from ...models import Device, DeviceState, ReachabilitySource

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor


# Source-precedence ledger. An observation can only override the
# current source when its priority is ≥ the recorded one;
# ``unknown`` at zero lets any source claim a yet-unlabelled device.
_SOURCE_PRIORITY: dict[str, int] = {
    ReachabilitySource.UNKNOWN: 0,
    ReachabilitySource.PING: 1,
    ReachabilitySource.MQTT: 2,
    ReachabilitySource.MDNS: 3,
}

# Per-sweep mDNS A-record resolve timeout — 3s keeps the whole
# pass under one ``_PING_INTERVAL`` even if every target misses
# the cache.
_MDNS_HOSTNAME_RESOLVE_TIMEOUT = 3.0


def should_ping(monitor: DeviceStateMonitor, device: Device) -> bool:
    """
    Decide whether *device* needs an ICMP probe this sweep.

    Skip the device only when it's already ONLINE *and* a
    higher-priority source (mDNS / MQTT) owns it. OFFLINE / UNKNOWN
    devices always get pinged so off-network hosts mDNS can't reach
    have a path to come online via DNS + ping.
    """
    if device.runtime_state.state != DeviceState.ONLINE:
        return True
    source = monitor.state.state_source.get(device.name, ReachabilitySource.UNKNOWN)
    return _SOURCE_PRIORITY.get(source, 0) <= _SOURCE_PRIORITY[ReachabilitySource.PING]


def apply_resolved_addresses(
    monitor: DeviceStateMonitor,
    name: str,
    addresses: list[str] | BaseException | None,
) -> None:
    """
    Funnel a successful active-resolve into the apply path.

    Deliberate no-OFFLINE-on-miss — see the rationale at the
    call site in :func:`resolve_non_api_mdns_targets`. ``addresses``
    accepts the ``BaseException | None`` union ``asyncio.gather(...,
    return_exceptions=True)`` produces.
    """
    if isinstance(addresses, list) and addresses:
        monitor.apply(name, DeviceState.ONLINE, "mdns", claim=True)
        monitor.apply_ip_addresses(name, addresses)


async def resolve_non_api_mdns_targets(monitor: DeviceStateMonitor) -> None:
    """
    Actively resolve ``.local`` hostnames for non-API devices.

    Devices whose YAML doesn't load the ``api`` integration
    (web_server / MQTT / OTA-only) never broadcast on
    ``_esphomelib._tcp.local.``, so the browser callback can't
    flip them ONLINE. On a quiet network where ICMP is also
    filtered they'd stay UNKNOWN forever. Issue an active A-record
    resolve for each such device every sweep so the indicator
    catches up. No-op when zeroconf failed to start.
    """
    zeroconf = monitor._mdns.zeroconf
    if zeroconf is None:
        return
    candidates = [
        d
        for d in monitor._get_devices()
        if d.address
        and is_local_hostname(d.address)
        and d.loaded_integrations
        and "api" not in d.loaded_integrations
        and should_ping(monitor, d)
    ]
    if not candidates:
        return
    results = await asyncio.gather(
        *(
            zeroconf.async_resolve_host(d.address, _MDNS_HOSTNAME_RESOLVE_TIMEOUT)
            for d in candidates
        ),
        return_exceptions=True,
    )
    for device, addresses in zip(candidates, results, strict=True):
        # Claim under the ``mdns`` source so the subsequent ICMP
        # sweep skips this device entirely — mDNS is the single
        # source of truth for devices that respond to it.
        apply_resolved_addresses(monitor, device.name, addresses)
        # No OFFLINE branch — deliberate. The browser path can
        # trust mDNS in both directions because the
        # ``ServiceBrowser`` delivers a ``Removed`` event on TTL
        # expiry. The one-off active resolve here has no such
        # subscription, so a miss conflates "device gone", "device
        # slow", and "transient packet loss"; let ICMP decide
        # instead.
