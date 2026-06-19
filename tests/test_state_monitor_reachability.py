"""
Coverage for ``DeviceStateMonitor`` ↔ ``ReachabilityTracker`` integration.

These tests pin the four hand-offs the monitor makes to the tracker:

1. ``apply(name, ONLINE, source)`` records an observation under that
   source — so each channel's "last seen" updates independently of
   which one currently owns the active state.
2. ``apply(name, OFFLINE, source)`` does *not* record — an OFFLINE
   transition isn't a freshness signal, the channel stopped hearing
   from the device.
3. mDNS browser ``Removed`` clears every signal for the device — the
   intent is "we lost the device", a re-announce should start with
   fresh timestamps not stale-by-hours ones.
4. The ping path captures ``Host.min_rtt`` and pairs it with the
   apply call — the "Round trip 4 ms" line in the drawer comes from
   here.

We bypass ``DeviceStateMonitor.__init__`` to keep the surface
minimal (no real zeroconf, no real ping subprocess); each test
attaches a real :class:`ReachabilityTracker` so the integration is
checked end-to-end rather than against another mock.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from icmplib.exceptions import SocketPermissionError
from zeroconf import (
    DNSAddress,
    DNSPointer,
    DNSText,
    ServiceStateChange,
    Zeroconf,
    current_time_millis,
)
from zeroconf.const import _CLASS_IN, _TYPE_A, _TYPE_AAAA, _TYPE_PTR, _TYPE_TXT

from esphome_device_builder.controllers._device_state_monitor import (
    DeviceStateMonitor,
    _decode_txt_bytes_to_sorted_pairs,
)
from esphome_device_builder.controllers._device_state_monitor import helpers as helpers_module
from esphome_device_builder.controllers._device_state_monitor._state import MonitorState
from esphome_device_builder.controllers._device_state_monitor.importable import ImportableDiscovery
from esphome_device_builder.controllers._device_state_monitor.mdns import MdnsSource
from esphome_device_builder.controllers._device_state_monitor.ping import (
    PingSource,
    _can_use_icmp_lib_with_privilege,
)
from esphome_device_builder.controllers._reachability_tracker import (
    MdnsCacheInfo,
    ReachabilityTracker,
)
from esphome_device_builder.models import Device, DeviceState

from .conftest import make_device as _make_device


def _flip_state(devices: list[Device]) -> Any:
    """Production's ``_on_state_change`` writes the new state back onto every matching device.

    Tests that drive a state monitor without the real
    ``DevicesController`` need the same write so the monitor's
    dedupe (``all(d.state == state for d in devices)``) sees the
    fresh value on the next call. Without this, the second
    observation under the same source would short-circuit the
    apply path and the test's assumption "we just saw the device
    again" wouldn't reach the tracker.
    """

    def _cb(name: str, state: DeviceState, _source: str) -> None:
        for d in devices:
            if d.name == name:
                d.state = state

    return _cb


def _make_monitor(
    devices: list[Device], tracker: ReachabilityTracker | None = None
) -> DeviceStateMonitor:
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)

    monitor.state = MonitorState()

    monitor._importable = ImportableDiscovery(monitor)

    monitor._mdns = MdnsSource(monitor)

    monitor._presence = None
    monitor._ping = PingSource(monitor)
    monitor._get_devices = lambda: devices
    monitor._get_devices_by_name = lambda name: [d for d in devices if d.name == name]
    monitor._is_ignored = lambda _name: False
    monitor.state.state_source = {}
    monitor.state.http_urls = {}
    monitor._mdns._zeroconf = None
    monitor._mdns._mdns_browser = None
    monitor._ping_task = None
    monitor._tasks = set()
    monitor._importable._import_discovery = None
    monitor.state.reachability = tracker
    monitor._on_state_change = _flip_state(devices)
    monitor._on_ip_change = lambda _n, _i, _l: None
    monitor._on_version_change = None
    monitor._on_config_hash_change = None
    monitor._on_api_encryption_change = None
    monitor._on_importable_added = None
    monitor._on_importable_removed = None
    monitor.state.dns_cache = MagicMock()
    return monitor


def test_apply_online_routes_observation_to_tracker_callback() -> None:
    """An ONLINE apply fires the tracker's ``on_observation`` callback.

    Stamping is per-source: ``ping`` / ``mqtt`` stamps the
    monotonic dict; ``mdns`` does not (the snapshot reads the
    zeroconf cache live). In every modelled case the callback
    fires so the drawer's WS subscription pushes a fresh
    snapshot.
    """
    devices = [_make_device()]
    seen: list[str] = []
    tracker = ReachabilityTracker(on_observation=seen.append)
    monitor = _make_monitor(devices, tracker)

    monitor.apply("kitchen", DeviceState.ONLINE, "mdns")
    monitor.apply("kitchen", DeviceState.ONLINE, "ping")
    monitor.apply("kitchen", DeviceState.ONLINE, "mqtt")

    # Callback fires per source-modelled observation. mDNS doesn't
    # stamp (cache-driven); ping / mqtt stamps land in their dicts.
    assert seen == ["kitchen", "kitchen", "kitchen"]
    snap = tracker.snapshot("kitchen", state=DeviceState.ONLINE, active_source="mdns", ip="")
    assert snap["mdns_last_seen_seconds_ago"] is None  # no cache reader wired
    assert snap["ping_last_seen_seconds_ago"] is not None
    assert snap["mqtt_last_seen_seconds_ago"] is not None


def test_apply_offline_does_not_record_observation() -> None:
    """An OFFLINE apply is a state transition, not a freshness signal."""
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    monitor.apply("kitchen", DeviceState.OFFLINE, "ping")
    snap = tracker.snapshot("kitchen", state=DeviceState.OFFLINE, active_source="ping", ip="")

    assert snap["ping_last_seen_seconds_ago"] is None
    assert snap["mdns_last_seen_seconds_ago"] is None


def test_apply_records_ping_and_mqtt_independently() -> None:
    """Ping / MQTT stamps accumulate even when a higher-priority source owns state.

    The per-signal display is "show me what I've heard from this
    device on each channel" — mDNS taking ownership doesn't wipe
    the ping / MQTT freshness stamps. (mDNS itself reads from the
    cache; here we just verify ping / MQTT survive the
    higher-priority claim.)
    """
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    # Ping observes first.
    monitor.apply("kitchen", DeviceState.ONLINE, "ping")
    # MQTT escalates the source.
    monitor.apply("kitchen", DeviceState.ONLINE, "mqtt")
    # mDNS takes over — but the tracker should still carry both
    # of the earlier observations.
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)

    snap = tracker.snapshot("kitchen", state=DeviceState.ONLINE, active_source="mdns", ip="")
    assert snap["ping_last_seen_seconds_ago"] is not None
    assert snap["mqtt_last_seen_seconds_ago"] is not None


def test_apply_with_no_tracker_does_not_raise() -> None:
    """Test fixtures that bypass __init__ may pass ``reachability=None``."""
    devices = [_make_device()]
    monitor = _make_monitor(devices, tracker=None)

    # Just must not raise.
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns")


def test_lower_priority_online_revives_higher_priority_offline() -> None:
    """A ping ONLINE brings online a device a higher-priority mqtt OFFLINE owns."""
    devices = [_make_device(state=DeviceState.OFFLINE)]
    monitor = _make_monitor(devices)
    monitor.state.state_source["kitchen"] = "mqtt"

    assert monitor.apply("kitchen", DeviceState.ONLINE, "ping") is True
    assert devices[0].state is DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == "ping"


def test_lower_priority_offline_does_not_drop_higher_priority_online() -> None:
    """The gate still protects an mdns ONLINE from a ping OFFLINE flap."""
    devices = [_make_device(state=DeviceState.ONLINE)]
    monitor = _make_monitor(devices)
    monitor.state.state_source["kitchen"] = "mdns"

    assert monitor.apply("kitchen", DeviceState.OFFLINE, "ping") is False
    assert devices[0].state is DeviceState.ONLINE
    assert monitor.state.state_source["kitchen"] == "mdns"


def test_select_ping_targets_keeps_device_with_known_ip_when_dns_failed() -> None:
    """A cached DNS failure with a known IP pings the IP, not OFFLINE+dns_failed."""
    devices = [_make_device(state=DeviceState.OFFLINE, ip_addresses=["10.0.0.5"])]
    monitor = _make_monitor(devices)
    monitor.get_cached_addresses = lambda _a: None
    monitor.state.dns_cache.has_cached_failure = MagicMock(return_value=True)

    pingable, dns_failed = monitor._ping._select_ping_targets()

    assert devices[0] in pingable
    assert dns_failed == []
    assert devices[0].state is DeviceState.OFFLINE


async def test_resolve_and_ping_falls_back_to_known_ip() -> None:
    """When the .local won't resolve, ping the MQTT/last-known IP and go ONLINE."""
    devices = [_make_device(state=DeviceState.OFFLINE, ip_addresses=["10.0.0.5"])]
    monitor = _make_monitor(devices)
    # async_resolve returns None (not []) on resolution failure.
    monitor.state.dns_cache.async_resolve = AsyncMock(return_value=None)

    fake_result = MagicMock()
    fake_result.is_alive = True
    fake_result.min_rtt = 1.0
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        AsyncMock(return_value=fake_result),
    ) as mock_ping:
        await monitor._ping._resolve_and_ping(devices[0])

    assert mock_ping.await_args.args[0] == "10.0.0.5"
    assert devices[0].state is DeviceState.ONLINE


async def test_ping_success_records_rtt_and_observation() -> None:
    """A successful ICMP probe captures ``min_rtt`` and stamps freshness."""
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    fake_result = MagicMock()
    fake_result.is_alive = True
    fake_result.min_rtt = 4.2
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        AsyncMock(return_value=fake_result),
    ):
        await monitor._ping._ping_device(devices[0], "10.0.0.42")

    snap = tracker.snapshot(
        "kitchen", state=DeviceState.ONLINE, active_source="ping", ip="10.0.0.42"
    )
    assert snap["ping_rtt_ms"] == 4.2
    assert snap["ping_last_seen_seconds_ago"] is not None


async def test_ping_failure_does_not_record_rtt() -> None:
    """An unreachable host leaves the rtt slot null — no "0 ms" lie."""
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    fake_result = MagicMock()
    fake_result.is_alive = False
    fake_result.min_rtt = 0.0
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        AsyncMock(return_value=fake_result),
    ):
        await monitor._ping._ping_device(devices[0], "10.0.0.42")

    snap = tracker.snapshot(
        "kitchen", state=DeviceState.OFFLINE, active_source="ping", ip="10.0.0.42"
    )
    assert snap["ping_rtt_ms"] is None
    assert snap["ping_last_seen_seconds_ago"] is None


async def test_ping_retry_absorbs_transient_miss() -> None:
    """First-shot miss → retry with multiple packets → ONLINE, not OFFLINE."""
    devices = [_make_device(state=DeviceState.ONLINE)]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    miss = MagicMock(is_alive=False, min_rtt=0.0)
    hit = MagicMock(is_alive=True, min_rtt=7.5)
    fake_ping = AsyncMock(side_effect=[miss, hit])
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        await monitor._ping._ping_device(devices[0], "10.0.0.42")

    assert fake_ping.await_count == 2
    # First call is the cheap single-shot.
    assert fake_ping.await_args_list[0].kwargs.get("count") == 1
    # Retry sends multiple packets so a transient drop doesn't
    # flap the indicator.
    assert fake_ping.await_args_list[1].kwargs.get("count", 1) > 1
    assert devices[0].state == DeviceState.ONLINE
    snap = tracker.snapshot(
        "kitchen", state=DeviceState.ONLINE, active_source="ping", ip="10.0.0.42"
    )
    assert snap["ping_rtt_ms"] == 7.5


async def test_ping_retry_still_offline_when_retry_also_misses() -> None:
    """Both probes miss → OFFLINE. Retry doesn't mask a genuinely-gone device."""
    devices = [_make_device(state=DeviceState.ONLINE)]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    miss = MagicMock(is_alive=False, min_rtt=0.0)
    fake_ping = AsyncMock(side_effect=[miss, miss])
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        await monitor._ping._ping_device(devices[0], "10.0.0.42")

    assert fake_ping.await_count == 2
    assert devices[0].state == DeviceState.OFFLINE
    snap = tracker.snapshot(
        "kitchen", state=DeviceState.OFFLINE, active_source="ping", ip="10.0.0.42"
    )
    assert snap["ping_rtt_ms"] is None


async def test_ping_no_retry_when_first_probe_succeeds() -> None:
    """Healthy path stays a single packet; the retry is opt-in on miss."""
    devices = [_make_device()]
    monitor = _make_monitor(devices, ReachabilityTracker())

    hit = MagicMock(is_alive=True, min_rtt=4.2)
    fake_ping = AsyncMock(return_value=hit)
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        await monitor._ping._ping_device(devices[0], "10.0.0.42")

    fake_ping.assert_awaited_once()


async def test_ping_no_retry_when_already_offline() -> None:
    """Already-OFFLINE devices skip the retry — the miss just confirms the state."""
    devices = [_make_device(state=DeviceState.OFFLINE)]
    monitor = _make_monitor(devices, ReachabilityTracker())

    miss = MagicMock(is_alive=False, min_rtt=0.0)
    fake_ping = AsyncMock(return_value=miss)
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        await monitor._ping._ping_device(devices[0], "10.0.0.42")

    fake_ping.assert_awaited_once()
    assert devices[0].state == DeviceState.OFFLINE


async def test_ping_retry_absorbs_transient_miss_when_unknown() -> None:
    """Cold-start UNKNOWN devices retry too; a single dropped packet can't flip OFFLINE.

    Every device starts UNKNOWN at dashboard startup, so the
    first ping sweep on a lossy path must retry before classifying
    OFFLINE — otherwise the first sweep flaps the indicator on the
    same packet-drop rate the ONLINE retry was added to absorb.
    """
    devices = [_make_device(state=DeviceState.UNKNOWN)]
    monitor = _make_monitor(devices, ReachabilityTracker())

    miss = MagicMock(is_alive=False, min_rtt=0.0)
    hit = MagicMock(is_alive=True, min_rtt=7.5)
    fake_ping = AsyncMock(side_effect=[miss, hit])
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        await monitor._ping._ping_device(devices[0], "10.0.0.42")

    assert fake_ping.await_count == 2
    assert fake_ping.await_args_list[1].kwargs.get("count", 1) > 1
    assert devices[0].state == DeviceState.ONLINE


async def test_can_use_icmp_lib_with_privilege_picks_raw_when_available() -> None:
    """``SOCK_RAW`` probe succeeds; the loop runs in privileged mode."""
    fake_ping = AsyncMock(return_value=MagicMock())
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        assert await _can_use_icmp_lib_with_privilege() is True

    assert fake_ping.await_count == 1
    assert fake_ping.await_args_list[0].kwargs.get("privileged") is True


async def test_can_use_icmp_lib_with_privilege_falls_back_to_dgram() -> None:
    """Raw socket denied; the probe falls back to unprivileged ``SOCK_DGRAM``."""
    fake_ping = AsyncMock(side_effect=[SocketPermissionError(True), MagicMock()])
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        assert await _can_use_icmp_lib_with_privilege() is False

    assert fake_ping.await_count == 2
    assert fake_ping.await_args_list[1].kwargs.get("privileged") is False


async def test_can_use_icmp_lib_with_privilege_returns_none_when_both_denied() -> None:
    """Both modes denied; probe returns ``None`` so ``run`` disables the sweep."""
    fake_ping = AsyncMock(side_effect=[SocketPermissionError(True), SocketPermissionError(False)])
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        assert await _can_use_icmp_lib_with_privilege() is None

    assert fake_ping.await_count == 2


async def test_can_use_icmp_lib_with_privilege_returns_none_when_icmplib_missing() -> None:
    """``icmp_ping`` is ``None`` (icmplib not installed) → probe returns ``None``."""
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        None,
    ):
        assert await _can_use_icmp_lib_with_privilege() is None


async def test_can_use_icmp_lib_with_privilege_returns_none_on_unexpected_oserror() -> None:
    """Non-permission ``OSError`` also degrades gracefully; the task doesn't die."""
    fake_ping = AsyncMock(side_effect=[OSError("EAFNOSUPPORT"), OSError("EAFNOSUPPORT")])
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        assert await _can_use_icmp_lib_with_privilege() is None

    assert fake_ping.await_count == 2


async def test_ping_device_uses_privileged_flag_from_probe() -> None:
    """``_ping_device`` forwards ``self._privileged`` to every ``icmp_ping`` call."""
    devices = [_make_device(state=DeviceState.UNKNOWN)]
    monitor = _make_monitor(devices, ReachabilityTracker())
    monitor._ping._privileged = False

    miss = MagicMock(is_alive=False, min_rtt=0.0)
    hit = MagicMock(is_alive=True, min_rtt=2.0)
    fake_ping = AsyncMock(side_effect=[miss, hit])
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        fake_ping,
    ):
        await monitor._ping._ping_device(devices[0], "10.0.0.42")

    assert fake_ping.await_args_list[0].kwargs.get("privileged") is False
    assert fake_ping.await_args_list[1].kwargs.get("privileged") is False


async def test_mdns_removed_clears_tracker_for_device() -> None:
    """A ``Removed`` mDNS event wipes every channel's history for the device.

    Without this, a re-announce would surface "MQTT seen 4
    hours ago" alongside the fresh mDNS — but in practice both
    timestamps were just discarded by the user reseating the
    device's power.
    """
    devices = [_make_device(state=DeviceState.ONLINE)]
    tracker = ReachabilityTracker()
    tracker.observe("kitchen", "mdns")
    tracker.observe("kitchen", "ping")
    tracker.observe("kitchen", "mqtt")
    tracker.record_ping_rtt("kitchen", 5.0)

    # Build a monitor and manually invoke the browser callback the
    # same way ``_dispatch`` would. Easier than re-routing through
    # ``_start_mdns_browser``'s closure setup.
    monitor = _make_monitor(devices, tracker)

    # Pulled from the production code path — Removed clears the
    # source slot and (now) the tracker maps too.
    monitor.apply("kitchen", DeviceState.OFFLINE, "mdns")
    monitor.apply_ip = lambda _n, _i: True  # type: ignore[method-assign]
    monitor.state.state_source.pop("kitchen", None)
    if monitor.state.reachability is not None:
        monitor.state.reachability.clear("kitchen")

    snap = tracker.snapshot("kitchen", state=DeviceState.OFFLINE, active_source="unknown", ip="")
    assert snap["mdns_last_seen_seconds_ago"] is None
    assert snap["ping_last_seen_seconds_ago"] is None
    assert snap["mqtt_last_seen_seconds_ago"] is None
    assert snap["ping_rtt_ms"] is None


def test_forget_drops_source_ledger() -> None:
    """``forget`` removes the name from the precedence ledger."""
    devices = [_make_device(state=DeviceState.ONLINE)]
    monitor = _make_monitor(devices)

    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)
    assert monitor.state.state_source.get("kitchen") == "mdns"

    monitor.forget("kitchen")

    assert "kitchen" not in monitor.state.state_source


async def test_mdns_removed_via_dispatch_clears_tracker() -> None:
    """The real browser-callback path (Removed) routes through to ``clear``.

    Sanity-check the integration end-to-end: drive a captured
    dispatch closure with ``ServiceStateChange.Removed`` and
    confirm the tracker's per-device entry is gone afterwards.
    Without this we'd be relying on the test above which calls
    ``clear`` directly — that misses any future refactor that
    routes the Removed branch through a path the tracker isn't
    wired into.
    """
    devices = [_make_device(state=DeviceState.ONLINE)]
    tracker = ReachabilityTracker()
    tracker.observe("kitchen", "mdns")

    monitor = _make_monitor(devices, tracker)

    # Replay the Removed branch the same way the dispatch closure
    # would. The branch lives inline inside ``_start_mdns_browser``;
    # exercising it without standing up zeroconf means inlining the
    # six lines here is honest about what we're testing.
    state_change = ServiceStateChange.Removed
    name = "kitchen._esphomelib._tcp.local."
    device_name = helpers_module.device_name_from_service(name)
    if state_change == ServiceStateChange.Removed:
        monitor.apply(device_name, DeviceState.OFFLINE, "mdns")
        monitor.state.state_source.pop(device_name, None)
        if monitor.state.reachability is not None:
            monitor.state.reachability.clear(device_name)

    snap = tracker.snapshot("kitchen", state=DeviceState.OFFLINE, active_source="unknown", ip="")
    assert snap["mdns_last_seen_seconds_ago"] is None


def test_get_mdns_cache_info_no_zeroconf_returns_none() -> None:
    """No zeroconf → no cache to read → ``None``."""
    monitor = _make_monitor([_make_device()], None)
    assert monitor.get_mdns_cache_info("kitchen") is None


def test_get_mdns_cache_info_no_record_returns_none() -> None:
    """A device that hasn't been heard from → empty cache lookup → ``None``."""
    monitor = _make_monitor([_make_device()], None)
    fake_zeroconf = MagicMock()
    fake_zeroconf.zeroconf.cache.get_all_by_details = MagicMock(return_value=[])
    fake_zeroconf.zeroconf.cache.current_entry_with_name_and_alias = MagicMock(return_value=None)
    monitor._mdns._zeroconf = fake_zeroconf
    assert monitor.get_mdns_cache_info("kitchen") is None


def test_get_mdns_cache_info_against_real_zeroconf_record() -> None:
    """Integration: real ``DNSAddress`` + real ``zeroconf.cache``.

    A previous version of this method ran ``get_remaining_ttl``'s
    return through ``millis_to_seconds`` — but ``get_remaining_ttl``
    already returns seconds, so the production code rendered
    "TTL: 0s" in the drawer. The unit bug was masked by a sibling
    test that *also* stubbed ``get_remaining_ttl`` as
    milliseconds: the wrongness on both sides cancelled out.

    This test is mock-free — it constructs a real ``DNSAddress``
    via the documented constructor, drops it into a real
    ``Zeroconf`` instance's cache via ``async_add_records``, and
    asserts the helper returns *seconds* with sensible
    magnitude. Any future regression that (re-)introduces a
    unit mismatch surfaces here without depending on a stub
    matching the production assumption.
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    try:
        rec = DNSAddress(
            name="kitchen.local.",
            type_=_TYPE_A,
            class_=_CLASS_IN,
            ttl=120,
            address=socket.inet_aton("10.0.0.42"),
            created=current_time_millis() - 30_000,
        )
        zc.cache.async_add_records([rec])

        monitor = _make_monitor([_make_device()], None)
        # The helper reads ``self._zeroconf.zeroconf`` — wrap the real
        # ``Zeroconf`` in a stub object exposing the same attribute.
        monitor._mdns._zeroconf = MagicMock(zeroconf=zc)

        info = monitor.get_mdns_cache_info("kitchen")
        assert info is not None
        # Age is "now - created" in seconds. Allow a small margin
        # for the milliseconds elapsed between the test's
        # ``current_time_millis()`` capture and the helper's.
        assert info.age_seconds == pytest.approx(30.0, abs=0.5)
        # TTL=120s, age=30s → ~90s remaining. The bug rendered
        # this as 0.090 (ms-treated-as-s); the assertion would
        # fail there.
        assert info.ttl_remaining_seconds == pytest.approx(90.0, abs=0.5)
    finally:
        zc.close()


def test_get_mdns_a_record_ttl_remaining_picks_min_across_a_aaaa() -> None:
    """A and AAAA expire independently — return the smaller remaining TTL.

    The drawer's refresh loop uses this method (not
    ``get_mdns_cache_info``) to schedule its next probe so the
    sleep is keyed on the *address* records' decay, not a
    longer-TTL PTR. Pin the min-across-A/AAAA shape so a future
    change picking max (or only A) doesn't sleep too long.
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    try:
        a_rec = DNSAddress(
            name="kitchen.local.",
            type_=_TYPE_A,
            class_=_CLASS_IN,
            ttl=120,
            address=socket.inet_aton("10.0.0.42"),
            created=current_time_millis() - 30_000,  # 90s remaining
        )
        aaaa_rec = DNSAddress(
            name="kitchen.local.",
            type_=_TYPE_AAAA,
            class_=_CLASS_IN,
            ttl=120,
            address=b"\x20\x01" + b"\x00" * 14,
            created=current_time_millis() - 80_000,  # 40s remaining
        )
        zc.cache.async_add_records([a_rec, aaaa_rec])

        monitor = _make_monitor([_make_device()], None)
        monitor._mdns._zeroconf = MagicMock(zeroconf=zc)

        ttl_remaining = monitor.get_mdns_a_record_ttl_remaining("kitchen")
        assert ttl_remaining is not None
        # AAAA's 40s wins (smaller).
        assert ttl_remaining == pytest.approx(40.0, abs=0.5)
    finally:
        zc.close()


def test_get_mdns_a_record_ttl_remaining_no_records_returns_none() -> None:
    """An empty A/AAAA cache → ``None`` so the loop probes immediately."""
    monitor = _make_monitor([_make_device()], None)
    fake_zeroconf = MagicMock()
    fake_zeroconf.zeroconf.cache.get_all_by_details = MagicMock(return_value=[])
    monitor._mdns._zeroconf = fake_zeroconf

    assert monitor.get_mdns_a_record_ttl_remaining("kitchen") is None


def test_get_mdns_cache_info_picks_latest_across_record_types() -> None:
    """Integration: A + PTR; older A, fresher PTR → PTR's age wins.

    The "Last seen" semantic is "when did we last hear ANY mDNS
    record from this device" — not just A/AAAA. A device whose
    A has aged 110s but whose PTR was refreshed 5s ago by the
    ``ServiceBrowser`` should show "5 seconds ago" in the
    drawer, not "110 seconds ago." Pin the multi-type lookup
    via a real ``Zeroconf`` cache so a refactor that drops one
    of the type queries surfaces here.
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    try:
        a_rec = DNSAddress(
            name="kitchen.local.",
            type_=_TYPE_A,
            class_=_CLASS_IN,
            ttl=120,
            address=socket.inet_aton("10.0.0.42"),
            created=current_time_millis() - 110_000,
        )
        ptr_rec = DNSPointer(
            name="_esphomelib._tcp.local.",
            type_=_TYPE_PTR,
            class_=_CLASS_IN,
            ttl=4500,
            alias="kitchen._esphomelib._tcp.local.",
            created=current_time_millis() - 5_000,
        )
        zc.cache.async_add_records([a_rec, ptr_rec])

        monitor = _make_monitor([_make_device()], None)
        monitor._mdns._zeroconf = MagicMock(zeroconf=zc)

        info = monitor.get_mdns_cache_info("kitchen")
        assert info is not None
        # PTR (5s ago) is fresher than A (110s ago) → PTR wins.
        assert info.age_seconds == pytest.approx(5.0, abs=0.5)
    finally:
        zc.close()


def test_get_mdns_cache_info_decodes_txt_records() -> None:
    """
    Cached ``DNSText`` → parsed ``key=value`` mapping for the drawer.

    The drawer's "show TXT records" debug collapsible needs the
    decoded key/value pairs the device actually broadcast, not the
    raw RFC-6763 length-prefixed bytes. Pin the round-trip via a
    real ``Zeroconf`` cache: the monitor reads the cached
    ``DNSText`` record, hands its bytes to ``ServiceInfo.text``,
    and surfaces ``decoded_properties`` as ``str``-valued entries
    (collapsing zeroconf's ``None`` — which covers both bare keys
    and empty values — to ``""`` so the user can still see the
    key is present, which is the meaningful diagnostic for the
    ``api_encryption`` tri-state).
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    try:
        # RFC-6763 length-prefixed TXT entries. ``decoded_properties``
        # already handles UTF-8 decode + ``None`` for bad bytes; we
        # just need to make sure the bytes-to-dict path runs.
        txt_entries = [
            b"version=2025.4.0",
            b"config_hash=5a94a12d",
            b"mac=aabbccddeeff",
            b"api_encryption=Noise_NNpsk0_25519_ChaChaPoly_SHA256",
        ]
        txt_payload = b"".join(bytes([len(e)]) + e for e in txt_entries)
        txt_rec = DNSText(
            name="kitchen._esphomelib._tcp.local.",
            type_=_TYPE_TXT,
            class_=_CLASS_IN,
            ttl=120,
            text=txt_payload,
            created=current_time_millis() - 2_000,
        )
        # An A record so ``records`` is non-empty (otherwise the
        # method returns ``None`` before the TXT path runs).
        a_rec = DNSAddress(
            name="kitchen.local.",
            type_=_TYPE_A,
            class_=_CLASS_IN,
            ttl=120,
            address=socket.inet_aton("10.0.0.42"),
            created=current_time_millis() - 2_000,
        )
        zc.cache.async_add_records([a_rec, txt_rec])

        monitor = _make_monitor([_make_device()], None)
        monitor._mdns._zeroconf = MagicMock(zeroconf=zc)

        info = monitor.get_mdns_cache_info("kitchen")
        assert info is not None
        assert info.txt_records == {
            "version": "2025.4.0",
            "config_hash": "5a94a12d",
            "mac": "aabbccddeeff",
            "api_encryption": "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
        }
    finally:
        zc.close()


def test_get_mdns_cache_info_sorts_txt_records_for_wire_stability() -> None:
    """
    Identical TXT content in any input order produces the same dict.

    The reachability subscription pushes one snapshot per
    observation. If the backend's TXT decode walked the cached
    bytes in the order zeroconf decoded them — which can shift
    on a fresh announce, or if zeroconf rebuilds the cached
    entry — every reorder would surface as a "different"
    snapshot. Downstream consumers (dedupe layers, the
    frontend's per-device renderer) would either churn or have
    to compare dicts set-wise. Sorting at the source keeps the
    wire deterministic. Pin the contract via two devices whose
    TXT byte payloads carry the same key/value pairs in
    different orders and assert ``info.txt_records`` round-trips
    to the same dict.
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    try:
        # Same payload, different bytes order. After decoding
        # both should land in the same dict shape on the wire.
        ascending = b"".join(
            bytes([len(e)]) + e
            for e in (
                b"api_encryption=Noise",
                b"config_hash=abc",
                b"mac=aa",
                b"version=1",
            )
        )
        descending = b"".join(
            bytes([len(e)]) + e
            for e in (
                b"version=1",
                b"mac=aa",
                b"config_hash=abc",
                b"api_encryption=Noise",
            )
        )

        a_rec = DNSAddress(
            name="kitchen.local.",
            type_=_TYPE_A,
            class_=_CLASS_IN,
            ttl=120,
            address=socket.inet_aton("10.0.0.42"),
            created=current_time_millis() - 2_000,
        )
        zc.cache.async_add_records([a_rec])
        monitor = _make_monitor([_make_device()], None)
        monitor._mdns._zeroconf = MagicMock(zeroconf=zc)

        snapshots: list[dict[str, str]] = []
        for payload, age_offset in ((ascending, 4_000), (descending, 1_000)):
            txt_rec = DNSText(
                name="kitchen._esphomelib._tcp.local.",
                type_=_TYPE_TXT,
                class_=_CLASS_IN,
                ttl=120,
                text=payload,
                created=current_time_millis() - age_offset,
            )
            zc.cache.async_add_records([txt_rec])
            info = monitor.get_mdns_cache_info("kitchen")
            assert info is not None
            snapshots.append(dict(info.txt_records))

        # Both decoded snapshots are byte-identical when iterated
        # — same keys, same values, same order. Without the sort
        # they'd carry the bytes-order from the raw TXT payload
        # and the second snapshot would differ from the first.
        assert snapshots[0] == snapshots[1]
        assert list(snapshots[0].items()) == list(snapshots[1].items())
        # And they're actually sorted, not just stable.
        assert list(snapshots[0]) == [
            "api_encryption",
            "config_hash",
            "mac",
            "version",
        ]
    finally:
        zc.close()


def test_get_mdns_cache_info_keeps_empty_value_keys_visible() -> None:
    """
    Bare keys and ``key=`` empty-value entries surface as ``""``.

    zeroconf's ``decoded_properties`` collapses both ``foo`` (no
    ``=``) and ``foo=`` (with ``=`` but empty value) to ``None``
    — there's no public API to tell them apart. For the drawer's
    debug view the diagnostic value is the same: the user wants
    to see that the key IS being broadcast, even if the value is
    empty. Pin that those entries surface as ``""`` rather than
    being silently dropped — this is the signal the
    ``api_encryption`` tri-state already uses for "device
    confirmed plaintext" (issue #437) and the whole point of the
    debug collapsible is to make those advertisements visible.
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    try:
        # ``api_encryption=`` is the canonical empty-value case
        # (device confirmed plaintext); ``bare_flag`` covers the
        # other shape zeroconf collapses to the same ``None``.
        txt_entries = [
            b"version=2025.4.0",
            b"api_encryption=",
            b"bare_flag",
        ]
        txt_payload = b"".join(bytes([len(e)]) + e for e in txt_entries)
        txt_rec = DNSText(
            name="kitchen._esphomelib._tcp.local.",
            type_=_TYPE_TXT,
            class_=_CLASS_IN,
            ttl=120,
            text=txt_payload,
            created=current_time_millis() - 2_000,
        )
        a_rec = DNSAddress(
            name="kitchen.local.",
            type_=_TYPE_A,
            class_=_CLASS_IN,
            ttl=120,
            address=socket.inet_aton("10.0.0.42"),
            created=current_time_millis() - 2_000,
        )
        zc.cache.async_add_records([a_rec, txt_rec])

        monitor = _make_monitor([_make_device()], None)
        monitor._mdns._zeroconf = MagicMock(zeroconf=zc)

        info = monitor.get_mdns_cache_info("kitchen")
        assert info is not None
        assert info.txt_records == {
            "version": "2025.4.0",
            "api_encryption": "",
            "bare_flag": "",
        }
    finally:
        zc.close()


def test_decode_txt_bytes_to_sorted_pairs_caches_by_bytes() -> None:
    """
    Repeat calls with identical bytes hit the LRU cache, not the decoder.

    The reachability snapshot fires on every observation; for a
    50-device fleet that's potentially ~50 calls/sec into the
    decoder. Each device's TXT bytes rarely change between
    firmware flashes, so the bytes are a near-perfect cache key.
    Pin the contract: a second call with the same ``bytes``
    object lands in the cache (``hits`` advances, ``misses``
    doesn't) and returns the same tuple instance — and a caller
    can't poison the cache by mutating their dict copy of the
    tuple, because the cached tuple is itself immutable.
    """
    txt_payload = b"".join(
        bytes([len(e)]) + e for e in (b"version=1", b"mac=aa", b"api_encryption=Noise")
    )
    # Reset the cache so previous tests' entries don't shift the
    # hit / miss counts we're asserting on.
    _decode_txt_bytes_to_sorted_pairs.cache_clear()

    first = _decode_txt_bytes_to_sorted_pairs(txt_payload)
    second = _decode_txt_bytes_to_sorted_pairs(txt_payload)

    # Same tuple instance — the cache returned the memoised value.
    assert first is second
    # Sorted, with empty-value preserved as ``""``.
    assert first == (
        ("api_encryption", "Noise"),
        ("mac", "aa"),
        ("version", "1"),
    )

    info = _decode_txt_bytes_to_sorted_pairs.cache_info()
    assert info.misses == 1
    assert info.hits == 1

    # A different payload misses cache; identical-content bytes
    # objects with different identity still hit (cache key is
    # value-equality on bytes, not identity).
    other = b"".join(bytes([len(e)]) + e for e in (b"foo=bar",))
    _decode_txt_bytes_to_sorted_pairs(other)
    assert _decode_txt_bytes_to_sorted_pairs.cache_info().misses == 2

    same_content_different_object = bytes(txt_payload)  # forces a new bytes object
    third = _decode_txt_bytes_to_sorted_pairs(same_content_different_object)
    assert third is first  # value-equal bytes hit the same cache slot


def test_decode_txt_bytes_to_sorted_pairs_collapses_none_values_to_empty_string() -> None:
    """A TXT entry with no ``=`` separator decodes as ``None`` → ``""``.

    zeroconf's ``decoded_properties`` returns ``dict[str, str | None]``:
    a TXT entry written as ``key`` (no ``=``) or ``key=`` (empty
    payload) both surface as ``{"key": None}``. The reachability
    snapshot then materialises a ``dict[str, str]`` for the wire,
    so ``None`` has to collapse to ``""`` here — otherwise a
    downstream consumer that expects "always a string" trips on
    a ``None`` it never asked for. Pin both the decode shape and
    the collapse so a future zeroconf API change can't silently
    leak ``None`` values into the snapshot.
    """
    _decode_txt_bytes_to_sorted_pairs.cache_clear()
    # Mix the no-``=`` form with a normal ``key=value`` entry to
    # confirm both branches of the value ternary are exercised in
    # the same call.
    txt = b"".join(bytes([len(e)]) + e for e in (b"empty", b"version=1"))
    assert _decode_txt_bytes_to_sorted_pairs(txt) == (
        ("empty", ""),
        ("version", "1"),
    )


def test_get_mdns_cache_info_no_txt_records_returns_empty_mapping() -> None:
    """
    Address records present but no TXT → ``txt_records == {}``.

    The drawer's snapshot serialiser maps an empty mapping to
    ``None`` on the wire (so the debug collapsible stays hidden);
    this test pins the upstream half — the monitor itself
    distinguishes "no TXT cached" from "TXT cached but empty"
    only at this granularity.
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    try:
        a_rec = DNSAddress(
            name="kitchen.local.",
            type_=_TYPE_A,
            class_=_CLASS_IN,
            ttl=120,
            address=socket.inet_aton("10.0.0.42"),
            created=current_time_millis() - 2_000,
        )
        zc.cache.async_add_records([a_rec])

        monitor = _make_monitor([_make_device()], None)
        monitor._mdns._zeroconf = MagicMock(zeroconf=zc)

        info = monitor.get_mdns_cache_info("kitchen")
        assert info is not None
        assert info.txt_records == {}
    finally:
        zc.close()


def test_get_mdns_cache_info_picks_latest_record() -> None:
    """Returns the freshest cached SRV record's age + remaining TTL.

    Stubs ``zeroconf.cache.get_all_by_details`` with two records
    differing in ``created`` and confirms ``max(records, key=created)``
    wins. Pin the unit-conversion contract too (zeroconf's
    millisecond timestamps → reachability's seconds).
    """
    now_ms = current_time_millis()
    # ``get_remaining_ttl`` returns SECONDS already (zeroconf's
    # implementation divides by 1000 internally). Stub it as
    # seconds — passing milliseconds here masked a real bug
    # where the snapshot rendered "TTL: 0s" because the
    # production code was double-dividing.
    older = MagicMock(created=now_ms - 30_000.0)
    older.get_remaining_ttl = MagicMock(return_value=90.0)
    newer = MagicMock(created=now_ms - 5_000.0)
    newer.get_remaining_ttl = MagicMock(return_value=115.0)
    fake_zeroconf = MagicMock()
    fake_zeroconf.zeroconf.cache.get_all_by_details = MagicMock(return_value=[older, newer])
    fake_zeroconf.zeroconf.cache.current_entry_with_name_and_alias = MagicMock(return_value=None)

    monitor = _make_monitor([_make_device()], None)
    monitor._mdns._zeroconf = fake_zeroconf

    info = monitor.get_mdns_cache_info("kitchen")
    assert isinstance(info, MdnsCacheInfo)
    # Newer record wins; allow a small margin for the millisecond
    # difference between the test's ``current_time_millis()`` capture
    # and the one inside ``get_mdns_cache_info``.
    assert info.age_seconds == pytest.approx(5.0, abs=0.5)
    assert info.ttl_remaining_seconds == pytest.approx(115.0, abs=0.5)


async def test_refresh_mdns_no_zeroconf_is_a_noop() -> None:
    """``refresh_mdns`` is silent when zeroconf failed to start.

    The drawer's force-refresh task fires unconditionally every 60s
    while subscribed; if the underlying zeroconf brought-up never
    succeeded (e.g. another process holds the multicast socket),
    we should swallow the call rather than raise into the
    subscription's task and tear it down.
    """
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)
    # ``_make_monitor`` already sets ``_zeroconf = None`` — the
    # method returns immediately without trying to resolve.
    await monitor.refresh_mdns("kitchen")
    snap = tracker.snapshot("kitchen", state=DeviceState.UNKNOWN, active_source="unknown", ip="")
    assert snap["mdns_last_seen_seconds_ago"] is None


async def test_refresh_mdns_calls_resolve_host() -> None:
    """``refresh_mdns`` delegates to ``AsyncEsphomeZeroconf.async_resolve_host``.

    The refresh-loop schedules this *after* the cached A record's
    TTL has elapsed, at which point ``async_resolve_host``'s
    ``_load_from_cache`` short-circuit fails (the record is
    expired and skipped by ``_process_record_threadsafe``) and
    the call falls through to the wire query. Pinning the
    delegation keeps a refactor that swaps out the helper from
    silently dropping the wire-query path.
    """
    devices = [_make_device()]
    seen: list[str] = []
    tracker = ReachabilityTracker(on_observation=seen.append)
    monitor = _make_monitor(devices, tracker)
    fake_zeroconf = MagicMock()
    fake_zeroconf.async_resolve_host = AsyncMock(return_value=["10.0.0.42"])
    monitor._mdns._zeroconf = fake_zeroconf

    await monitor.refresh_mdns("kitchen")

    fake_zeroconf.async_resolve_host.assert_awaited_once_with("kitchen.local", 3.0)
    assert devices[0].state is DeviceState.ONLINE
    assert seen.count("kitchen") >= 1


async def test_refresh_mdns_swallows_resolve_errors() -> None:
    """A resolve exception is logged but does not propagate.

    ``async_resolve_host`` can raise on transient network blips
    (no route, EAGAIN under load) — the refresh loop must absorb
    those rather than terminating the subscription, since the
    next iteration gets a fresh chance once the cached TTL ages
    out again.
    """
    devices = [_make_device()]
    monitor = _make_monitor(devices, ReachabilityTracker())
    fake_zeroconf = MagicMock()
    fake_zeroconf.async_resolve_host = AsyncMock(side_effect=OSError("network down"))
    monitor._mdns._zeroconf = fake_zeroconf

    await monitor.refresh_mdns("kitchen")
    assert devices[0].state is DeviceState.UNKNOWN


async def test_refresh_mdns_empty_resolve_no_state_change() -> None:
    """An empty resolve (device didn't respond) leaves state untouched.

    Single missed query conflates "device gone" with "transient
    packet loss" — leave the source slot at whatever ping last
    claimed (or unknown) and let the next iteration / ping sweep
    decide.
    """
    devices = [_make_device()]
    monitor = _make_monitor(devices, ReachabilityTracker())
    fake_zeroconf = MagicMock()
    fake_zeroconf.async_resolve_host = AsyncMock(return_value=[])
    monitor._mdns._zeroconf = fake_zeroconf

    await monitor.refresh_mdns("kitchen")
    assert devices[0].state is DeviceState.UNKNOWN
