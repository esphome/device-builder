"""
Offloader-side mDNS peer discovery.

Hosts the :class:`AsyncServiceBrowser` lifecycle and the
service-state-change callback that maintains the
``offloader._peers`` dict and fires
:attr:`EventType.REMOTE_BUILD_HOST_ADDED` /
:attr:`EventType.REMOTE_BUILD_HOST_REMOVED`. Bodies take the
:class:`OffloaderController` as the first arg; the controller
keeps thin bound-method delegates so test patches against
``controller.offloader._on_service_state_change`` and the
:attr:`_browser` lifecycle continue to resolve.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ...helpers.dashboard_advertise import SERVICE_TYPE
from ...models import EventType, RemoteBuildHostRemovedData
from ._mdns import endpoints_equal, peer_from_service_info

if TYPE_CHECKING:
    from .offloader import OffloaderController

_LOGGER = logging.getLogger(__name__)

# Cache-miss resolve timeout for the dashboard service-info
# fetch. Longer than the device-state monitor's because peer
# dashboards run on full hosts that may be more LAN hops away.
_RESOLVE_TIMEOUT_MS = 3000


def start_discovery(controller: OffloaderController) -> None:
    """
    Bring up the mDNS service browser for peer discovery.

    Captures the dashboard's own service-instance name (so
    our own advertise doesn't show up in ``list_hosts``) and
    constructs the :class:`AsyncServiceBrowser` against the
    shared zeroconf. Skips silently if either the devices
    controller or its zeroconf isn't available (peer
    discovery is opt-in fail-soft); on browser-construction
    failure logs the exception and leaves :attr:`_browser`
    as ``None``.
    """
    if controller._db.devices is None:
        _LOGGER.debug("remote-build discovery skipped: devices controller unavailable")
        return
    zeroconf = controller._db.devices.zeroconf
    if zeroconf is None:
        _LOGGER.debug("remote-build discovery skipped: zeroconf unavailable")
        return
    # Capture own service-instance name so our own advertise
    # doesn't show up in ``list_hosts``. Reads through the
    # public ``service_instance_name`` accessor on
    # ``DashboardAdvertiser`` rather than reaching into
    # ``_info``; keeps this controller decoupled from the
    # advertiser's private layout.
    advertiser = controller._db._dashboard_advertiser
    if advertiser is not None:
        controller.state.own_instance_name = advertiser.service_instance_name
    # Wrap browser construction so a zeroconf-side failure
    # (e.g. the underlying socket got torn down between
    # ``DeviceStateMonitor.start`` and now, or the cache is in
    # an unexpected state) doesn't abort dashboard startup.
    try:
        controller.state.browser = AsyncServiceBrowser(
            zeroconf.zeroconf,
            [SERVICE_TYPE],
            handlers=[controller._on_service_state_change],
        )
    except Exception:
        _LOGGER.exception("Could not start remote-build browser; peer discovery disabled")
        controller.state.browser = None


def on_service_state_change(
    controller: OffloaderController,
    zeroconf: Any,
    service_type: str,
    name: str,
    state_change: ServiceStateChange,
) -> None:
    """
    Browser callback; resolve the service info and update the peer map.

    Filters our own service-instance name so we don't surface
    our own advertise as a discovered host. ``Removed`` events
    delete the peer immediately and fire
    :attr:`EventType.REMOTE_BUILD_HOST_REMOVED`; ``Added`` /
    ``Updated`` resolve either from the zeroconf cache (sync,
    fires :attr:`EventType.REMOTE_BUILD_HOST_ADDED` inline) or
    via a fire-and-forget task (async, fires from
    :meth:`_resolve_and_apply` once the SRV / TXT round-trip
    completes).
    """
    if name == controller.state.own_instance_name:
        return
    if state_change == ServiceStateChange.Removed:
        popped = controller.state.peers.pop(name, None)
        if popped is not None:
            # Event keys on the wire-friendly ``peer.name``
            # (leftmost label) so frontend dicts keyed on the
            # ``RemoteBuildPeer.name`` field upsert/delete
            # consistently. The FQDN ``name`` is the
            # ``controller.state.peers`` dict key only.
            controller._fire_host_removed(popped.name)
        return
    info = AsyncServiceInfo(service_type, name)
    if info.load_from_cache(zeroconf):
        controller._upsert_host(name, info)
        return
    controller._track_task(controller._resolve_and_apply(zeroconf, info, name))


async def resolve_and_apply(
    controller: OffloaderController, zeroconf: Any, info: AsyncServiceInfo, name: str
) -> None:
    """Async resolve path for cache misses."""
    try:
        resolved = await info.async_request(zeroconf, timeout=_RESOLVE_TIMEOUT_MS)
    except Exception:
        _LOGGER.debug("Resolve failed for %s", name, exc_info=True)
        return
    if not resolved:
        return
    controller._upsert_host(name, info)


def upsert_host(controller: OffloaderController, name: str, info: AsyncServiceInfo) -> None:
    """Replace the row keyed on *name* and fire ``REMOTE_BUILD_HOST_ADDED``.

    Drops entries whose ``(server, port)`` matches our own
    advertise — the instance-name filter handles the common
    case, but a rename-on-conflict bounce can leave the
    captured name stale.
    """
    peer = peer_from_service_info(name, info)
    if controller._is_self_endpoint(peer.hostname, peer.port):
        return
    controller.state.peers[name] = peer
    controller._db.bus.fire(EventType.REMOTE_BUILD_HOST_ADDED, peer.to_dict())
    # mDNS auto-rebind: if this broadcast's pin matches a
    # stored pairing whose ``(host, port)`` differs, the
    # probe-then-rebind background task verifies the new
    # endpoint really is our paired receiver before mutating.
    controller._maybe_schedule_rebind_probe(peer)


def is_self_endpoint(controller: OffloaderController, hostname: str, port: int) -> bool:
    """Return True when *(hostname, port)* matches our published advertise.

    Reads the live ``service_target_endpoint`` off the
    :class:`DashboardAdvertiser` rather than a captured-at-start
    value so a post-start register / re-register isn't missed.
    Hostname comparison goes through
    :func:`normalize_hostname` so the resolved peer hostname
    and the advertiser's published target compare equal
    regardless of trailing-dot / case.
    """
    advertiser = controller._db._dashboard_advertiser
    if advertiser is None:
        return False
    endpoint = advertiser.service_target_endpoint
    if endpoint is None:
        return False
    own_host, own_port = endpoint
    return endpoints_equal(hostname, port, own_host, own_port)


def fire_host_removed(controller: OffloaderController, name: str) -> None:
    """Fire ``REMOTE_BUILD_HOST_REMOVED`` for *name*."""
    payload: RemoteBuildHostRemovedData = {"name": name}
    controller._db.bus.fire(EventType.REMOTE_BUILD_HOST_REMOVED, payload)
