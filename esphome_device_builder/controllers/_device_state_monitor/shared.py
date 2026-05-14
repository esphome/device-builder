"""
Cross-cutting helpers used by both the mdns browser path and the ping source.

Lives outside both ``mdns.py`` and ``ping.py`` because each function
straddles concerns:

* ``resolve_non_api_mdns_targets`` issues active mDNS resolves but
  runs in the ping loop's pre-sweep step, and is gated by the same
  ``should_ping`` rule the ICMP path consults.
* ``apply_resolved_addresses`` funnels both the browser-callback
  refresh path and the active-resolve batch into ``monitor.apply``
  with the same "non-empty list → claim mDNS-ONLINE + record IPs"
  treatment.
* ``should_ping`` and the ``_SOURCE_PRIORITY`` ledger are read by
  the central ``apply()`` write path AND by both modules above.

Free functions taking the monitor — same shape as the
firmware-sync helpers. ``DeviceStateMonitor`` reaches back through
``state`` for everything sibling modules need.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ...helpers.hostname import is_local_hostname
from ...models import Device, DeviceState, ReachabilitySource

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor


# Source priority for state observations. A new observation can only
# override an existing one when its priority is greater than or equal
# to the current source's. Keep ``unknown`` at zero so any source can
# claim a device that no source has yet labelled.
_SOURCE_PRIORITY: dict[str, int] = {
    ReachabilitySource.UNKNOWN: 0,
    ReachabilitySource.PING: 1,
    ReachabilitySource.MQTT: 2,
    ReachabilitySource.MDNS: 3,
}

# Timeout for the per-sweep mDNS hostname resolves we issue for
# non-API devices. 3s is enough on a working LAN even when the
# device is briefly slow to respond, and keeps the whole resolve
# pass under the ping interval even if every target misses the
# cache and has to round-trip on the network.
_MDNS_HOSTNAME_RESOLVE_TIMEOUT = 3.0


def should_ping(monitor: DeviceStateMonitor, device: Device) -> bool:
    """
    Decide whether *device* needs an ICMP probe this sweep.

    Mirrors the upstream dashboard's rule: skip the device only when
    it's already ONLINE *and* a higher-priority source (mDNS / MQTT)
    owns it. We still ping devices that are OFFLINE or UNKNOWN so an
    off-network host — one mDNS can't reach because it's on a
    different subnet — has a path to come online via DNS + ping.
    """
    if device.state != DeviceState.ONLINE:
        return True
    source = monitor.state.state_source.get(device.name, ReachabilitySource.UNKNOWN)
    return _SOURCE_PRIORITY.get(source, 0) <= _SOURCE_PRIORITY[ReachabilitySource.PING]


def apply_resolved_addresses(
    monitor: DeviceStateMonitor,
    name: str,
    addresses: list[str] | BaseException | None,
) -> None:
    """Funnel a successful active-resolve into the apply path.

    Both the per-subscription :meth:`refresh_mdns` and the batch
    :func:`resolve_non_api_mdns_targets` need the same "non-empty
    address list → claim mDNS-ONLINE + record IPs" treatment.
    Sharing the branch keeps the deliberate no-OFFLINE-on-miss rule
    (documented at the call site in
    :func:`resolve_non_api_mdns_targets`) consistent across both
    paths.

    ``addresses`` accepts the union ``asyncio.gather(...,
    return_exceptions=True)`` produces so the batch path can thread
    its results in without a per-element type check.
    """
    if isinstance(addresses, list) and addresses:
        monitor.apply(name, DeviceState.ONLINE, "mdns", claim=True)
        monitor.apply_ip_addresses(name, addresses)


async def resolve_non_api_mdns_targets(monitor: DeviceStateMonitor) -> None:
    """Actively resolve ``.local`` hostnames for non-API devices.

    Devices whose YAML doesn't load the ``api`` integration
    (web_server-only, MQTT-only, OTA-only configs) never broadcast
    on ``_esphomelib._tcp.local.`` so the browser callback never
    fires for them. The cache-based fallback in the ping target
    selector only catches them when the zeroconf A-record cache
    happens to be primed (e.g. by an unrelated query). On a quiet
    network where ICMP is also filtered (some corporate / HA
    setups), those devices stay UNKNOWN forever even though
    they're reachable.

    Issue an active mDNS A-record resolve for each non-API device
    every sweep so the indicator flips ONLINE even without an
    esphomelib service announcement. Mirrors the legacy
    dashboard's ``async_refresh_hosts`` poll path
    (``esphome/dashboard/status/mdns.py``). No-op when the
    zeroconf browser failed to start.
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
        # Trust mDNS for ONLINE — the active A-record query
        # answered, so the device is live on this LAN. Claim
        # under the ``mdns`` source (priority 3) so the
        # subsequent ICMP sweep skips this device entirely.
        # Keeping ping / DNS traffic to a minimum for fleets
        # that broadcast is a deliberate trade-off: we want
        # mDNS to be the single source of truth for devices
        # that respond to it.
        apply_resolved_addresses(monitor, device.name, addresses)
        # No OFFLINE branch — deliberate. The browser path can
        # trust mDNS in both directions because the
        # ServiceBrowser delivers a ``Removed`` event when a
        # cached record's TTL expires without renewal; that's
        # the canonical "I'm gone" signal. The one-off active
        # resolve we run here has no such subscription — a
        # miss is just "this single query didn't get a reply
        # in time", which conflates "device gone", "device
        # slow", and "transient packet loss". Falling back to
        # ICMP for the OFFLINE decision in this path is the
        # right shape: an mDNS hit upgrades to mDNS-owned
        # ONLINE; a miss leaves the source slot at whatever
        # ping last claimed (or unknown), and ping decides.
