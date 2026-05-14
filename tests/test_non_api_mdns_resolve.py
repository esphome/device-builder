"""Tests for the non-API mDNS hostname-resolve fallback.

Devices whose YAML doesn't load the ``api`` integration
(web_server-only, MQTT-only, OTA-only configs) never broadcast on
``_esphomelib._tcp.local.``. The state monitor's
``ServiceBrowser`` callback never fires for them, and on networks
where ICMP is filtered the ping fallback can't pick them up
either — the indicator stays UNKNOWN forever even though the
device is reachable.

``_resolve_non_api_mdns_targets`` issues an active mDNS A-record
query every sweep for that subset of devices. Mirrors the legacy
dashboard's ``async_refresh_hosts`` path
(``esphome/dashboard/status/mdns.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, DeviceState

from .conftest import make_state_monitor_with_callbacks


def _device(
    name: str = "kitchen",
    *,
    address: str = "kitchen.local",
    loaded_integrations: list[str] | None = None,
    state: DeviceState = DeviceState.UNKNOWN,
) -> Device:
    return Device(
        name=name,
        friendly_name=name,
        configuration=f"{name}.yaml",
        address=address,
        loaded_integrations=loaded_integrations or [],
        state=state,
    )


def _make_monitor(
    devices: list[Device], resolved: dict[str, list[str] | None] | None = None
) -> tuple[DeviceStateMonitor, AsyncMock]:
    """Build a monitor with a mocked ``AsyncEsphomeZeroconf``.

    Wires the production-mirroring state/IP write-back via the
    shared :class:`RecordingMonitorCallbacks` (the recorder's
    ``calls`` list isn't read by these tests — they assert on the
    device's own ``state`` / ``ip`` after the side-effect runs —
    but it's the canonical way to get the write-back for free).

    ``resolved`` maps a hostname to either a list of IPs (resolve
    succeeded), an empty list / ``None`` (resolve failed). The
    mock's ``async_resolve_host`` looks up the host in the dict
    and returns the value verbatim. Tests that need exception
    behaviour or per-call dynamics replace the mock's
    ``side_effect`` directly (see
    ``test_resolve_exception_does_not_propagate`` /
    ``test_multiple_devices_resolve_in_parallel``).
    """
    monitor, _callbacks = make_state_monitor_with_callbacks(devices)

    fake_zc = MagicMock()
    resolve_map = resolved or {}

    async def _resolve(host: str, _timeout: float = 0) -> list[str] | None:
        return resolve_map.get(host)

    fake_zc.async_resolve_host = AsyncMock(side_effect=_resolve)
    monitor._zeroconf = fake_zc
    return monitor, fake_zc.async_resolve_host


@pytest.mark.asyncio
async def test_non_api_device_marked_online_when_mdns_resolves() -> None:
    """A web-server-only YAML flips ONLINE when mDNS resolves its hostname.

    The browser path doesn't fire for non-API devices (they never
    broadcast esphomelib). Without this fallback, on networks
    where ICMP is filtered the device would stay UNKNOWN forever.
    """
    devices = [_device(loaded_integrations=["web_server"])]
    monitor, resolver = _make_monitor(devices, resolved={"kitchen.local": ["192.168.1.42"]})

    await monitor._resolve_non_api_mdns_targets()

    assert devices[0].state == DeviceState.ONLINE
    assert devices[0].ip == "192.168.1.42"
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_device_skipped() -> None:
    """API-loaded devices go through the browser path, not the resolve fallback.

    Without this filter we'd issue a redundant A-record query for
    every esphomelib-broadcasting device on every sweep — wasted
    work that scales linearly with fleet size.
    """
    devices = [_device(loaded_integrations=["api", "web_server"])]
    monitor, resolver = _make_monitor(devices)

    await monitor._resolve_non_api_mdns_targets()

    resolver.assert_not_called()
    assert devices[0].state == DeviceState.UNKNOWN


@pytest.mark.asyncio
async def test_uncompiled_device_skipped() -> None:
    """Devices with empty ``loaded_integrations`` skip the resolve.

    A YAML that's been added but never compiled has no
    ``loaded_integrations`` populated yet (StorageJSON hasn't been
    written). We can't tell whether it'll end up with ``api`` or
    not, so don't preemptively resolve — wait until
    ``--only-generate`` finishes and StorageJSON tells us.
    """
    devices = [_device(loaded_integrations=[])]
    monitor, resolver = _make_monitor(devices)

    await monitor._resolve_non_api_mdns_targets()

    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_non_local_hostname_skipped() -> None:
    """Resolve only kicks in for ``.local`` hostnames — DNS is the right tool elsewhere."""
    devices = [_device(address="device.example.com", loaded_integrations=["mqtt"])]
    monitor, resolver = _make_monitor(devices)

    await monitor._resolve_non_api_mdns_targets()

    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_miss_is_silent_no_offline_branch_by_design() -> None:
    """A miss is silent — ONLY trust mDNS for ONLINE, never for OFFLINE.

    Deliberate asymmetry: a successful active mDNS resolve is
    strong evidence the device is live, but a miss isn't strong
    evidence it's gone (could be a transient broadcast pause, a
    flaky network, or just a slow device responding past the
    timeout). Claiming OFFLINE on miss would lock out the ICMP
    fallback for the device under ``mdns`` priority and leave
    devices on flaky networks stuck red. Stay silent and let
    ICMP — which is unblocked while no source has claimed the
    slot — decide.
    """
    devices = [_device(loaded_integrations=["web_server"])]
    monitor, _resolver = _make_monitor(devices, resolved={"kitchen.local": None})

    await monitor._resolve_non_api_mdns_targets()

    assert devices[0].state == DeviceState.UNKNOWN  # not OFFLINE
    assert devices[0].ip == ""
    # Source slot stays empty so ping (priority 1) can claim
    # later — the resolve hasn't earned ownership yet.
    assert monitor.priority_for("kitchen") == "unknown"


@pytest.mark.asyncio
async def test_resolve_exception_does_not_propagate() -> None:
    """A zeroconf exception on one host doesn't poison the others.

    Real ``async_resolve_host`` calls can fail with ``OSError`` /
    network errors mid-sweep. Use ``return_exceptions=True`` so a
    single bad host doesn't cancel the gather, and skip-quietly on
    a non-list result so the next sweep retries normally.
    """
    devices = [
        _device(name="kitchen", loaded_integrations=["web_server"]),
        _device(name="bedroom", address="bedroom.local", loaded_integrations=["mqtt"]),
    ]
    monitor, _ = _make_monitor(devices)

    async def _resolve(host: str, _timeout: float = 0) -> list[str] | None:
        if host == "kitchen.local":
            raise OSError("simulated zeroconf failure")
        return ["192.168.1.50"]

    monitor._zeroconf.async_resolve_host = AsyncMock(side_effect=_resolve)

    await monitor._resolve_non_api_mdns_targets()

    # bedroom resolves cleanly even though kitchen blew up.
    bedroom = next(d for d in devices if d.name == "bedroom")
    assert bedroom.state == DeviceState.ONLINE
    assert bedroom.ip == "192.168.1.50"
    # kitchen stayed UNKNOWN — the exception didn't get re-raised
    # as a state mutation.
    kitchen = next(d for d in devices if d.name == "kitchen")
    assert kitchen.state == DeviceState.UNKNOWN


@pytest.mark.asyncio
async def test_no_zeroconf_is_a_noop() -> None:
    """Pre-start (or zeroconf-failed) sweep must not raise."""
    devices = [_device(loaded_integrations=["web_server"])]
    monitor, _ = _make_monitor(devices)
    monitor._zeroconf = None  # simulate ``async_setup`` failure

    # No exception, no state change.
    await monitor._resolve_non_api_mdns_targets()
    assert devices[0].state == DeviceState.UNKNOWN


@pytest.mark.asyncio
async def test_already_online_via_higher_priority_skipped() -> None:
    """A device claimed by mDNS / MQTT already doesn't get re-resolved.

    ``_should_ping`` returns False for ONLINE devices owned by a
    source whose priority is above ``ping``. We piggyback on the
    same predicate: if mDNS / MQTT already won, an extra resolve
    on the next sweep would just spam queries for nothing.
    """
    devices = [_device(loaded_integrations=["web_server"], state=DeviceState.ONLINE)]
    monitor, resolver = _make_monitor(devices)
    monitor.state.state_source["kitchen"] = "mdns"  # higher than ping

    await monitor._resolve_non_api_mdns_targets()

    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_hit_locks_out_ping() -> None:
    """A resolve hit claims ``mdns`` priority and ICMP stops.

    Pings cost network / DNS traffic. Once mDNS has answered, we
    treat that as the source of truth — locking ICMP out via the
    ``mdns`` priority claim avoids the redundant probe on every
    subsequent sweep. The trade-off is that a device going offline
    after mDNS first claimed it stays ONLINE until either the
    fleet operator restarts the dashboard or another (higher-
    priority) source contradicts the claim; that's deliberate
    given how often quiet devices skip a single mDNS broadcast.
    """
    devices = [_device(loaded_integrations=["web_server"])]
    monitor, _ = _make_monitor(devices, resolved={"kitchen.local": ["192.168.1.42"]})

    await monitor._resolve_non_api_mdns_targets()

    assert devices[0].state == DeviceState.ONLINE
    assert monitor.priority_for("kitchen") == "mdns"
    assert monitor._should_ping(devices[0]) is False


@pytest.mark.asyncio
async def test_offline_via_ping_still_resolved() -> None:
    """A device flipped OFFLINE by ping is still a resolve candidate.

    Ping is the lower-priority source. If mDNS later reports the
    device live, we want the resolver pass to upgrade ONLINE on
    the next sweep. ``_should_ping`` returns True for non-ONLINE
    devices regardless of source.
    """
    devices = [_device(loaded_integrations=["web_server"], state=DeviceState.OFFLINE)]
    monitor, resolver = _make_monitor(devices, resolved={"kitchen.local": ["192.168.1.42"]})
    monitor.state.state_source["kitchen"] = "ping"

    await monitor._resolve_non_api_mdns_targets()

    resolver.assert_awaited_once()
    assert devices[0].state == DeviceState.ONLINE


@pytest.mark.asyncio
async def test_resolve_picks_ipv4_for_apply_ip() -> None:
    """Active resolve forwards every IP; primary picks IPv4 when present.

    ``Device.ip`` only carries one address — cross-subnet ICMP and
    OTA cache args both prefer V4 — but ``Device.ip_addresses``
    keeps the full announced set so the dashboard can surface every
    IP a multi-homed device claims.
    """
    devices = [_device(loaded_integrations=["web_server"])]
    monitor, _ = _make_monitor(
        devices,
        resolved={"kitchen.local": ["fe80::1%en0", "192.168.1.42", "fe80::2%en0"]},
    )

    await monitor._resolve_non_api_mdns_targets()

    assert devices[0].ip == "192.168.1.42"
    assert devices[0].ip_addresses == ["fe80::1%en0", "192.168.1.42", "fe80::2%en0"]


@pytest.mark.asyncio
async def test_no_candidates_skips_zeroconf_call() -> None:
    """All API-loaded fleet → no resolve work.

    Cheap pre-filter: don't even allocate the gather if every
    matching device is excluded. Verified by asserting the
    resolver mock was never called (a real call would be O(N)
    against the live zeroconf instance).
    """
    devices = [_device(loaded_integrations=["api"])]
    monitor, resolver = _make_monitor(devices)

    await monitor._resolve_non_api_mdns_targets()

    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_multiple_devices_resolve_in_parallel(monkeypatch: Any) -> None:
    """All non-API devices resolve in a single ``asyncio.gather`` call.

    Sequential resolution would serialise N devices behind one
    another; with concurrent resolves the whole sweep finishes
    inside the timeout window of the slowest single request.
    Verified by counting concurrent invocations of the mocked
    ``async_resolve_host`` — it should be called once per device,
    all dispatched before any awaits return.
    """
    devices = [
        _device(name="kitchen", loaded_integrations=["mqtt"]),
        _device(name="bedroom", address="bedroom.local", loaded_integrations=["mqtt"]),
        _device(name="garage", address="garage.local", loaded_integrations=["mqtt"]),
    ]
    monitor, _ = _make_monitor(devices)

    pending = 0
    max_concurrent = 0

    async def _resolve(host: str, _timeout: float = 0) -> list[str] | None:
        nonlocal pending, max_concurrent
        pending += 1
        max_concurrent = max(max_concurrent, pending)
        try:
            await asyncio.sleep(0)  # let other coroutines start
            return ["10.0.0.1"]
        finally:
            pending -= 1

    monitor._zeroconf.async_resolve_host = AsyncMock(side_effect=_resolve)

    await monitor._resolve_non_api_mdns_targets()

    assert max_concurrent == 3
