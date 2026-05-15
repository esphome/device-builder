"""Lifecycle + browser-callback coverage for ``DeviceStateMonitor``.

This file fills in the branches the per-feature suites
(``test_mdns_*``, ``test_probe_device``, ``test_non_api_mdns_resolve``)
don't reach. Tests drive through the public API
(``start`` / ``stop`` / ``probe_device`` / ``revisit_*`` / the
``apply_*`` family) plus the dispatch closure that
``AsyncServiceBrowser`` would invoke in production. The closure is
captured by patching ``AsyncServiceBrowser`` so the test owns the
``handlers=[...]`` list; that's the same boundary the real
zeroconf would call across, so calling it directly from a test is
the legitimate way to drive the browser-callback graph.

The monitor talks to ``zeroconf`` and ``icmplib`` heavily, so every
test stubs those out. Construction goes through ``__new__`` +
manual attribute assignment to avoid spinning up a real
``AsyncEsphomeZeroconf`` on every test.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zeroconf import ServiceStateChange

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.controllers._device_state_monitor import importable as importable_module
from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module
from esphome_device_builder.controllers._device_state_monitor import ping as ping_module
from esphome_device_builder.controllers._device_state_monitor._state import MonitorState
from esphome_device_builder.controllers._device_state_monitor.importable import ImportableDiscovery
from esphome_device_builder.controllers._device_state_monitor.mdns import MdnsSource
from esphome_device_builder.controllers._device_state_monitor.ping import PingSource
from esphome_device_builder.controllers._reachability_tracker import ReachabilityTracker
from esphome_device_builder.models import Device, DeviceState

from .conftest import RecordingMonitorCallbacks

# The service-type strings the production code uses; pinned here so
# tests calling the captured dispatch use the exact same constants
# the code under test does.
ESPHOMELIB_SERVICE_TYPE = "_esphomelib._tcp.local."
HTTP_SERVICE_TYPE = "_http._tcp.local."


def _device(
    *,
    name: str = "kitchen",
    address: str = "kitchen.local",
    state: DeviceState = DeviceState.UNKNOWN,
    **overrides: Any,
) -> Device:
    """Build a Device with sensible defaults — only the fields the monitor reads."""
    return Device(
        name=name,
        friendly_name=name.title(),
        configuration=f"{name}.yaml",
        address=address,
        state=state,
        **overrides,
    )


def _make_monitor(
    devices: list[Device] | None = None,
) -> tuple[DeviceStateMonitor, RecordingMonitorCallbacks]:
    """Build a monitor shell with the apply-* callbacks recorded.

    Bypasses ``__init__`` so tests don't have to feed the full
    callback set or hit the real zeroconf bring-up. Wires a shared
    :class:`RecordingMonitorCallbacks` for every ``_on_*`` slot so
    the production state-flip side-effect (each callback writes
    the broadcast value back onto every matching Device) runs
    inline — the monitor's own dedupe
    (``_any_matching_device_differs``) then sees the updated state
    on the second call instead of repeating itself.

    Returns ``(monitor, callbacks)`` so tests assert on
    ``callbacks.calls`` directly instead of poking
    ``MagicMock.assert_called_*`` on each separate slot.
    """
    devices = list(devices) if devices is not None else [_device()]
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)

    monitor.state = MonitorState()

    monitor._importable = ImportableDiscovery(monitor)

    monitor._mdns = MdnsSource(monitor)

    monitor._presence = None  # ping loop runs unconditionally in tests
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

    callbacks = RecordingMonitorCallbacks(devices)
    monitor._on_state_change = callbacks.on_state_change
    monitor._on_ip_change = callbacks.on_ip_change
    monitor._on_version_change = callbacks.on_version_change
    monitor._on_config_hash_change = callbacks.on_config_hash_change
    monitor._on_api_encryption_change = callbacks.on_api_encryption_change
    monitor._on_mac_address_change = callbacks.on_mac_address_change
    monitor._on_importable_added = callbacks.on_importable_added
    monitor._on_importable_removed = callbacks.on_importable_removed
    monitor.state.reachability = None
    monitor.state.dns_cache = MagicMock()
    return monitor, callbacks


async def _start_with_captured_dispatch(
    monitor: DeviceStateMonitor,
    monkeypatch: pytest.MonkeyPatch,
    *,
    import_discovery: Any | None = None,
    park_ping_loop: bool = True,
) -> Any:
    """Run ``monitor.start`` while capturing the registered browser dispatch.

    The dispatch handler is a closure inside ``_start_mdns_browser``;
    the legitimate way to invoke it in a test is the same way the
    real zeroconf does — by getting a reference to the callable the
    monitor passed to ``AsyncServiceBrowser(handlers=[...])``. We
    patch ``AsyncServiceBrowser`` to capture that argument, then run
    ``start`` (the public API) and return the captured callable so
    each test can fire its own ``ServiceStateChange.*`` events.
    """
    captured: dict[str, Any] = {}
    fake_zeroconf = MagicMock()
    fake_zeroconf.zeroconf = MagicMock()
    monkeypatch.setattr(mdns_module, "AsyncEsphomeZeroconf", lambda: fake_zeroconf)
    monkeypatch.setattr(
        importable_module,
        "DashboardImportDiscovery",
        lambda _cb: (
            import_discovery
            if import_discovery is not None
            else MagicMock(browser_callback=lambda *_a, **_kw: None, import_state={})
        ),
    )

    def _capture(_zeroconf: Any, _types: Any, *, handlers: list[Any]) -> Any:
        captured["dispatch"] = handlers[0]
        return MagicMock(async_cancel=AsyncMock())

    monkeypatch.setattr(mdns_module, "AsyncServiceBrowser", _capture)

    # Park the ping loop forever so the bootstrap sleep + first
    # sweep don't fire during browser-only tests. Ping-pipeline
    # tests pass ``park_ping_loop=False`` so the production loop
    # runs end-to-end and the test can drive it via patched
    # ``asyncio.sleep``.
    if park_ping_loop:

        async def _park() -> None:
            await asyncio.sleep(60)

        monkeypatch.setattr(monitor, "_ping_loop", _park, raising=False)

    await monitor.start()
    return captured["dispatch"]


async def _let_ping_loop_run_briefly(monitor: DeviceStateMonitor) -> None:
    """Yield long enough for the ping loop (paired with ``_shrink_ping_intervals``) to sweep."""
    if monitor._ping_task is None:
        return
    await asyncio.sleep(0.05)


async def _stop_and_drain(monitor: DeviceStateMonitor) -> None:
    """Stop the monitor and await every task it cancelled.

    ``stop`` cancels the ping task and the in-flight resolve
    tasks but doesn't await them — by the time it returns,
    ``monitor._ping_task`` is ``None``. Without grabbing a
    reference first, the cancelled task can survive past the
    test as a "Task was destroyed but it is pending!" warning
    on loop teardown. Hold the reference, run ``stop``, then
    await the cancellation cleanly.
    """
    ping_task = monitor._ping_task
    in_flight = list(monitor._tasks)
    await monitor.stop()
    pending = [t for t in [ping_task, *in_flight] if t is not None]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_every_async_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    """``start`` → ``stop`` cancels the ping task, browser, in-flight resolves, and zeroconf.

    Drives the full lifecycle through the public API. The
    ``AsyncServiceBrowser`` capture stub returns a MagicMock whose
    ``async_cancel`` is an AsyncMock so the cancel call is real,
    not just a no-op.
    """
    monitor, _callbacks = _make_monitor()
    await _start_with_captured_dispatch(monitor, monkeypatch)

    # Add a fake in-flight resolve task so stop has something to drain.
    async def _long_running() -> None:
        await asyncio.sleep(60)

    in_flight = asyncio.create_task(_long_running())
    monitor._tasks.add(in_flight)
    # Replace the zeroconf with one that has an awaitable async_close.
    monitor._mdns._zeroconf = MagicMock()
    monitor._mdns._zeroconf.async_close = AsyncMock()

    await _stop_and_drain(monitor)

    assert monitor._ping_task is None
    assert monitor._mdns._mdns_browser is None
    assert monitor._mdns._zeroconf is None
    assert monitor._tasks == set()
    assert in_flight.cancelled() or in_flight.done()


@pytest.mark.asyncio
async def test_stop_swallows_browser_cancel_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise from the browser teardown is logged at debug, not propagated.

    ``stop`` runs during application shutdown; an exception from
    browser teardown can't be allowed to leak into aiohttp's
    cleanup chain or the user-visible exit hangs.
    """
    monitor, _callbacks = _make_monitor()
    await _start_with_captured_dispatch(monitor, monkeypatch)
    monitor._mdns._mdns_browser.async_cancel = AsyncMock(side_effect=RuntimeError("browser broke"))
    monitor._mdns._zeroconf.async_close = AsyncMock()

    await _stop_and_drain(monitor)  # must not raise

    assert monitor._mdns._mdns_browser is None


@pytest.mark.asyncio
async def test_stop_swallows_zeroconf_close_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``zeroconf.async_close`` raise also lands at debug, not the caller."""
    monitor, _callbacks = _make_monitor()
    await _start_with_captured_dispatch(monitor, monkeypatch)
    monitor._mdns._zeroconf.async_close = AsyncMock(side_effect=RuntimeError("zeroconf broke"))

    await _stop_and_drain(monitor)  # must not raise

    assert monitor._mdns._zeroconf is None


# ---------------------------------------------------------------------------
# start() — failure fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_falls_back_when_zeroconf_construct_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AsyncEsphomeZeroconf()`` raising → log + ping-only fallback.

    Some hosts (Docker without --net=host, restrictive sandboxes)
    can't bring up zeroconf. The monitor must still run the ping
    sweep so the dashboard isn't blind, hence the catch-and-continue.
    """
    monitor, _callbacks = _make_monitor()

    def _boom() -> None:
        raise RuntimeError("no zeroconf for you")

    monkeypatch.setattr(mdns_module, "AsyncEsphomeZeroconf", _boom)

    async def _park() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(monitor, "_ping_loop", _park, raising=False)

    await monitor.start()
    try:
        assert monitor._mdns._zeroconf is None
        assert monitor._mdns._mdns_browser is None
        # Ping task is still running — we want OFFLINE detection
        # even without zeroconf.
        assert monitor._ping_task is not None
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_start_continues_when_browser_construct_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AsyncServiceBrowser()`` raising leaves zeroconf up but no live announcements.

    ``_zeroconf`` stays set so ``probe_device`` and the cache
    helpers still work; only the live announcement stream is lost,
    and ping covers it.
    """
    monitor, _callbacks = _make_monitor()
    fake_zeroconf = MagicMock()
    fake_zeroconf.zeroconf = MagicMock()
    fake_zeroconf.async_close = AsyncMock()
    monkeypatch.setattr(mdns_module, "AsyncEsphomeZeroconf", lambda: fake_zeroconf)
    monkeypatch.setattr(
        importable_module,
        "DashboardImportDiscovery",
        lambda _cb: MagicMock(),
    )

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("browser broke")

    monkeypatch.setattr(mdns_module, "AsyncServiceBrowser", _boom)

    async def _park() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(monitor, "_ping_loop", _park, raising=False)

    await monitor.start()
    try:
        assert monitor._mdns._zeroconf is fake_zeroconf
        assert monitor._mdns._mdns_browser is None
    finally:
        await _stop_and_drain(monitor)


# ---------------------------------------------------------------------------
# Browser dispatch — Removed / cache-hit / cache-miss / unconfigured / HTTP route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_removed_event_flips_offline_clears_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``Removed`` esphomelib event flips OFFLINE, clears IP, drops source slot."""
    device = _device(state=DeviceState.ONLINE, ip="10.0.0.1")
    monitor, _callbacks = _make_monitor([device])
    monitor.state.state_source["kitchen"] = "mdns"
    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Removed,
        )
        assert device.state == DeviceState.OFFLINE
        assert device.ip == ""
        assert "kitchen" not in monitor.state.state_source
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_removed_event_clears_reachability_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch path's Removed branch wipes the tracker for the device.

    Drives the actual browser dispatch closure (rather than
    replaying the branch by hand) so a future refactor that
    relocates the ``self._reachability.clear(device_name)`` call
    out of the closure is caught here. The other reachability
    tests cover the helper-level contract; this one pins the
    end-to-end edge.
    """
    device = _device(state=DeviceState.ONLINE)
    monitor, _callbacks = _make_monitor([device])
    tracker = ReachabilityTracker()
    monitor.state.reachability = tracker
    tracker.observe("kitchen", "mdns")
    tracker.observe("kitchen", "ping")
    monitor.state.state_source["kitchen"] = "mdns"
    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Removed,
        )
        snap = tracker.snapshot(
            "kitchen", state=DeviceState.OFFLINE, active_source="unknown", ip=""
        )
        assert snap["mdns_last_seen_seconds_ago"] is None
        assert snap["ping_last_seen_seconds_ago"] is None
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_cache_hit_propagates_full_txt_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-hit Added event applies IPs + version + config_hash + api_encryption.

    Anchors the full bundle so a refactor that drops one of the
    apply-* calls (or reorders in a way that hides a missing one)
    surfaces here. Also the V4-primary preference: ``device.ip``
    holds the IPv4 even when the info also carries V6, while
    ``device.ip_addresses`` keeps every announced address.
    """
    device = _device()
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    # ``_apply_service_info`` calls ``parsed_scoped_addresses(IPVersion.All)``
    # — return the full announced set (V4 + V6) so we can assert the
    # primary picks V4 and the full list lands on the device.
    fake_info.parsed_scoped_addresses = lambda _mode: ["10.0.0.5", "fe80::1%en0"]
    fake_info.decoded_properties = {
        "version": "2026.5.0",
        "config_hash": "abcd1234",
        "api_encryption": "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
    }
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )

        assert device.state == DeviceState.ONLINE
        assert device.ip == "10.0.0.5"
        assert device.ip_addresses == ["10.0.0.5", "fe80::1%en0"]
        assert device.deployed_version == "2026.5.0"
        assert device.deployed_config_hash == "abcd1234"
        assert device.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
        assert monitor._tasks == set()
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_cache_hit_falls_back_to_v6_when_no_v4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V6-only announcement → primary is the first scoped V6 address.

    Pin the V4-preference fallback in ``_pick_ipv4``: with no IPv4
    in the announced set, ``device.ip`` lands on the first V6 entry
    so the OTA cache args still have a target. The full list flows
    through to ``ip_addresses`` either way.
    """
    device = _device(state=DeviceState.UNKNOWN)
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.parsed_scoped_addresses = lambda _mode: ["fe80::1%en0"]
    fake_info.decoded_properties = {}
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert device.ip == "fe80::1%en0"
        assert device.ip_addresses == ["fe80::1%en0"]
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_with_explicit_empty_api_encryption_pushes_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TXT key explicitly empty means plaintext-confirmed → pushed as empty string.

    The tri-state on the model side is ``"…"`` (encrypted) /
    ``""`` (confirmed plaintext) / ``None`` (never observed). An
    *explicit* empty TXT value is a real signal — the device is
    advertising the key but with no value, which esphome emits when
    encryption is genuinely off in the running firmware. Pin that
    we still hand that down to ``apply_api_encryption``.
    """
    device = _device(api_encryption_active=None)
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.parsed_scoped_addresses = lambda _mode: []
    fake_info.decoded_properties = {"api_encryption": ""}
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert device.api_encryption_active == ""
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_without_api_encryption_txt_preserves_last_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TXT key absent in the announcement → current value is preserved.

    Regression test for the "switching to ping clears
    ``api_encryption`` and the dashboard prompts to reinstall" bug.
    The two states ``""`` (TXT explicitly empty — plaintext
    confirmed) and ``None`` (TXT key absent in *this* announcement)
    used to collapse to the same applied ``""`` via
    ``props.get("api_encryption") or ""``, so a transient /
    fragmented re-announcement that omitted the TXT silently
    overwrote a previously-truthy value with ``""`` and flipped the
    frontend's lock indicator to "mismatch" / "pending → reinstall".

    Now: TXT absent is treated as "no signal in this announcement",
    and the device's last-known value (``"Noise…"`` here) survives.
    """
    truthy = "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    device = _device(api_encryption_active=truthy)
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.parsed_scoped_addresses = lambda _mode: []
    fake_info.decoded_properties = {}  # no api_encryption key at all
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        # Truthy survives.
        assert device.api_encryption_active == truthy
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_without_api_encryption_txt_keeps_unknown_at_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TXT key absent + device starts at ``None`` → stays at ``None``.

    Older firmwares that never broadcast the TXT (pre-encryption-TXT
    rollout) leave the device at the ``None`` initial — the
    frontend's ``getEncryptionState`` falls back to the YAML's
    ``api_encrypted`` flag in that case, which is the right
    behaviour for a device whose actual encryption state is
    genuinely unknowable from mDNS alone.
    """
    device = _device(api_encryption_active=None)
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.parsed_scoped_addresses = lambda _mode: []
    fake_info.decoded_properties = {}
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert device.api_encryption_active is None
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_api_encryption_absent_with_other_content_clears_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TXT carries other keys but ``api_encryption`` is missing → confirm plaintext.

    The firmware was rebuilt without encryption and is
    re-announcing its real new state. ESPHome's TXT broadcasts
    are atomic per announce — there's no fragmentation shape
    that would carry ``version`` / ``mac`` / ``config_hash``
    but drop only ``api_encryption`` — so the absence of the
    key inside an otherwise-populated TXT IS authoritative for
    "encryption was removed."

    Pre-fix: the old guard ``if api_encryption is not None``
    treated this case identically to a transient empty-fragment
    (no apply, previous truthy value preserved). The result
    was a stale lock indicator that stayed green long after
    the device's firmware was downgraded to plaintext.
    """
    truthy = "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    device = _device(api_encryption_active=truthy)
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.parsed_scoped_addresses = lambda _mode: []
    # Real-shaped TXT: version/mac/config_hash present, but
    # ``api_encryption`` absent. ESPHome firmware that was
    # rebuilt without encryption broadcasts exactly this shape.
    fake_info.decoded_properties = {
        "version": "2026.4.0",
        "mac": "aabbccddeeff",
        "config_hash": "abc12345",
    }
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        # Wire authoritatively says no encryption — flip to
        # confirmed-plaintext so the lock indicator follows.
        assert device.api_encryption_active == ""
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_api_encryption_bare_key_pushes_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare-key TXT (``api_encryption`` with no ``=`` value) → confirm plaintext.

    zeroconf collapses bare keys (``foo``) and empty-value
    entries (``foo=``) to the same ``None`` in
    ``decoded_properties``. Both shapes are how ESPHome firmware
    broadcasts "I have the key in my TXT but the value slot
    is empty" — the documented confirmed-plaintext signal.

    Pre-fix: ``props.get(...) is not None`` returned False for
    ``None`` so the apply was skipped — a latent bug where the
    confirmed-plaintext signal wasn't actually flowing through.
    Now the explicit ``in props`` check catches the key
    presence and treats the ``None`` value as the empty-string
    plaintext signal.
    """
    truthy = "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    device = _device(api_encryption_active=truthy)
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.parsed_scoped_addresses = lambda _mode: []
    # Mirror what zeroconf actually returns for ``api_encryption=``
    # or bare ``api_encryption``: the key is present in the dict
    # but the value is ``None``.
    fake_info.decoded_properties = {"api_encryption": None}
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert device.api_encryption_active == ""
    finally:
        await _stop_and_drain(monitor)


# ---------------------------------------------------------------------------
# Defense-in-depth: TXT-absent / TXT-empty must preserve the device's
# last-known value for every mDNS-derived field that doesn't have an
# "explicit empty is meaningful" semantic (i.e. everything except
# ``api_encryption`` — its plaintext-confirmed signal is covered above).
#
# Today these fields are protected by the truthy walrus at the call site
# (``if version := props.get(...)``) plus an empty-guard at the bottom
# of each ``apply_*`` method. Pinning the contract here means a future
# refactor that "simplifies" away either guard surfaces as a test
# failure rather than a silent regression on a quiet re-announce
# (MTU fragmentation, source flipping mDNS → ping mid-flight, post-OTA
# propagation, …).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("device_field", "txt_key", "stored_value"),
    [
        # Field on the Device, the TXT key it's read from, and a
        # representative stored value to round-trip through the test.
        # ``version`` drives the "Update available" pill;
        # ``config_hash`` drives the "running firmware out of sync"
        # indicator (paired with ``expected_config_hash``); ``mac``
        # drives the drawer's primary-MAC row + the derived
        # ethernet/bluetooth rows on ESP32.
        ("deployed_version", "version", "2026.5.0"),
        ("deployed_config_hash", "config_hash", "5a94a12d"),
        ("mac_address", "mac", "94:C9:60:1F:8C:F1"),
    ],
)
@pytest.mark.parametrize(
    ("make_props", "case_id"),
    [
        # Absent: a sparse re-announcement that didn't carry the TXT
        # at all (the canonical fragmentation / OTA-propagation case).
        (lambda _key: {}, "absent"),
        # Explicitly empty: TXT key present but with empty value.
        # No meaningful semantic for these fields (unlike
        # ``api_encryption`` where empty = "plaintext confirmed"),
        # so empty is treated the same as absent — drop it and keep
        # the canonical value.
        (lambda key: {key: ""}, "empty"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
@pytest.mark.asyncio
async def test_dispatch_added_sparse_announce_preserves_last_known(
    device_field: str,
    txt_key: str,
    stored_value: str,
    make_props,
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TXT-absent / TXT-empty announcements preserve the stored field.

    Six cases (3 fields x {absent, empty}). With either the call-
    site walrus or the apply-method empty-guard intact the field
    survives the announcement unchanged; if a future refactor
    drops both, at least one of these parametrize legs fails
    loudly.
    """
    device = _device(**{device_field: stored_value})
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.parsed_scoped_addresses = lambda _mode: []
    fake_info.decoded_properties = make_props(txt_key)
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert getattr(device, device_field) == stored_value, (
            f"{device_field} wiped by sparse announce ({case_id})"
        )
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_cache_miss_resolves_and_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache miss → fire-and-forget resolve task that applies the populated info.

    Drives the cache-miss path end-to-end: dispatch fires, the task
    spawns, ``async_request`` returns True, ``_apply_service_info``
    runs, and the device picks up the version. Awaiting the spawned
    task is what exercises the real ``_resolve_then`` path (no
    direct call to ``_resolve_then`` in the test).
    """
    device = _device()
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    fake_info.async_request = AsyncMock(return_value=True)
    fake_info.parsed_scoped_addresses = lambda _mode: ["10.0.0.5"]
    fake_info.decoded_properties = {"version": "2026.5.0"}
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert len(monitor._tasks) == 1
        await asyncio.gather(*list(monitor._tasks))
        assert device.deployed_version == "2026.5.0"
        assert device.ip == "10.0.0.5"
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_cache_miss_skips_apply_when_request_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``async_request`` False (no record arrived in time) → no apply, device unchanged.

    Pins the ``if not await info.async_request: return`` branch in
    ``_resolve_then`` from the public side: the device's version
    stays empty because no apply ever ran.
    """
    device = _device()
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    fake_info.async_request = AsyncMock(return_value=False)
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        await asyncio.gather(*list(monitor._tasks))
        assert device.deployed_version == ""
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_cache_miss_swallows_resolve_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise from ``async_request`` is caught and logged at debug.

    Production trigger: zeroconf occasionally raises a transient
    ``OSError`` (network restart, interface flap). The fire-and-
    forget task must not propagate — there's no caller to handle it.
    """
    device = _device()
    monitor, _callbacks = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    fake_info.async_request = AsyncMock(side_effect=OSError("network flap"))
    monkeypatch.setattr(mdns_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        await asyncio.gather(*list(monitor._tasks), return_exceptions=True)
        # No raise reached the test, and the device wasn't updated.
        assert device.deployed_version == ""
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_skips_unconfigured_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An mDNS event for a device not in the catalog is dropped silently.

    The dashboard sees every ``_esphomelib`` broadcast on the LAN —
    we don't want to spawn lookups or apply state for unrelated
    nodes that just happen to share the air.
    """
    monitor, callbacks = _make_monitor()  # only "kitchen" configured
    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"stranger.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert callbacks.calls_for("on_state_change") == []
        assert monitor._tasks == set()
    finally:
        await _stop_and_drain(monitor)


# ---------------------------------------------------------------------------
# Browser dispatch — HTTP service path
# ---------------------------------------------------------------------------


def _make_import_discovery(discoveries: dict[str, Any] | None = None) -> Any:
    """Build a stand-in ``DashboardImportDiscovery``.

    Only the two attributes the production code uses are stubbed:
    ``import_state`` (a dict-like the monitor walks for the
    revisit and HTTP-refire paths) and ``browser_callback``
    (called from the dispatch shim — a no-op in tests).
    Discoveries are pre-populated so tests can assert that a
    later HTTP event re-fires for the right importable name.
    """
    discovery = MagicMock()
    discovery.import_state = discoveries or {}
    discovery.browser_callback = lambda *_a, **_kw: None
    return discovery


def _build_discovered(name: str, **overrides: Any) -> Any:
    """Build a fake ``DiscoveredImport`` carrying just the fields we read."""
    discovered = MagicMock()
    discovered.device_name = name
    discovered.friendly_name = overrides.get("friendly_name", name.title())
    discovered.package_import_url = overrides.get(
        "package_import_url", f"github://example/{name}.yaml"
    )
    discovered.project_name = overrides.get("project_name", "example.proj")
    discovered.project_version = overrides.get("project_version", "1.0.0")
    discovered.network = overrides.get("network", "wifi")
    return discovered


@pytest.mark.asyncio
async def test_dispatch_http_service_added_records_url_and_refires_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP service for an importable name records the URL and re-fires.

    Drives the ``_http._tcp.local.`` branch of the dispatch shim
    end-to-end: the import_discovery is pre-populated with a
    matching importable; the HTTP event fires; the monitor stores
    the URL and calls ``on_importable_added`` again so the
    frontend picks up the ``web_url`` change.
    """
    monitor, callbacks = _make_monitor(devices=[])
    discovered = _build_discovered("factory-firmware")
    discovery = _make_import_discovery({f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": discovered})

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.server = "factory-firmware.local."
    fake_info.port = 8080
    monkeypatch.setattr(importable_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"factory-firmware.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert monitor.state.http_urls["factory-firmware"] == ("http://factory-firmware.local:8080")
        # The importable was re-emitted with the new web_url.
        assert any(call[1].web_url for call in callbacks.calls_for("on_importable_added"))
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_http_service_added_skips_when_no_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP service for an unrelated name doesn't pollute ``_http_urls``.

    Without this guard ``_http_urls`` would grow unbounded from
    every HTTP service on the LAN (printers, NAS boxes, routers).
    """
    monitor, callbacks = _make_monitor(devices=[])
    discovery = _make_import_discovery()  # no discoveries

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.server = "stranger.local."
    fake_info.port = 80
    monkeypatch.setattr(importable_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"stranger.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert monitor.state.http_urls == {}
        assert callbacks.calls_for("on_importable_added") == []
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_http_service_removed_clears_url_and_refires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removed HTTP service drops the cached URL and re-fires the importable.

    The refire walks ``_on_import_update`` → ``_seed_http_url_from_cache``,
    which constructs an ``AsyncServiceInfo`` and asks zeroconf's
    cache for the latest. Stub it here as a cache miss — the URL
    we asserted was already populated, and the seed path is meant
    to be a no-op when the user-visible state already has it.
    """
    monitor, callbacks = _make_monitor(devices=[])
    discovered = _build_discovered("factory-firmware")
    discovery = _make_import_discovery({f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": discovered})
    monitor.state.http_urls["factory-firmware"] = "http://factory-firmware.local"

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    monkeypatch.setattr(importable_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"factory-firmware.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Removed,
        )
        assert "factory-firmware" not in monitor.state.http_urls
        assert callbacks.calls_for("on_importable_added")
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_http_service_removed_for_untracked_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing an HTTP service we never tracked → no fan-out at all."""
    monitor, callbacks = _make_monitor(devices=[])
    discovery = _make_import_discovery()

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"stranger.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Removed,
        )
        assert callbacks.calls_for("on_importable_added") == []
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_http_service_added_cache_miss_resolves_and_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache-miss HTTP service spawns a resolve task that fills the URL on success."""
    monitor, _callbacks = _make_monitor(devices=[])
    discovered = _build_discovered("factory-firmware")
    discovery = _make_import_discovery({f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": discovered})

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    fake_info.async_request = AsyncMock(return_value=True)
    fake_info.server = "factory-firmware.local."
    fake_info.port = 80
    monkeypatch.setattr(importable_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._mdns._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"factory-firmware.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert len(monitor._tasks) == 1
        await asyncio.gather(*list(monitor._tasks))
        assert monitor.state.http_urls["factory-firmware"] == "http://factory-firmware.local"
    finally:
        await _stop_and_drain(monitor)


# ---------------------------------------------------------------------------
# revisit_importable / revisit_all_importables / get_importable_devices
# ---------------------------------------------------------------------------


def test_revisit_all_importables_no_op_when_discovery_not_running() -> None:
    """Pre-start (zeroconf-down) monitor short-circuits without iterating.

    Pin the early-return branch so a refactor that drops the
    ``import_discovery is None`` guard doesn't ``AttributeError``
    before the dashboard's mDNS browser has come up.
    """
    monitor, callbacks = _make_monitor()
    monitor._importable._import_discovery = None

    monitor.revisit_all_importables()  # must not raise

    assert callbacks.calls_for("on_importable_added") == []


def test_revisit_all_importables_re_emits_every_cached_entry() -> None:
    """``revisit_all_importables`` walks every entry and re-fires the ADD callback."""
    monitor, callbacks = _make_monitor(devices=[])
    monitor._importable._import_discovery = _make_import_discovery(
        {
            f"a.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("a"),
            f"b.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("b"),
        }
    )

    monitor.revisit_all_importables()

    emitted = {call[1].name for call in callbacks.calls_for("on_importable_added")}
    assert emitted == {"a", "b"}


def test_revisit_importable_seeds_url_from_cache_when_http_already_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the HTTP service was cached before the importable arrived, the URL still seeds.

    Late-binding: the HTTP browser callback skipped storing the
    URL because no importable existed for that name yet. Now
    ``revisit_importable`` runs (e.g. after a configured device
    was deleted, freeing the name), and the
    ``_seed_http_url_from_cache`` hook inside ``_on_import_update``
    pulls the URL out of zeroconf's cache so the emitted
    ``AdoptableDevice`` carries the link from the first event.
    """
    monitor, callbacks = _make_monitor(devices=[])
    monitor._mdns._zeroconf = MagicMock()
    monitor._mdns._zeroconf.zeroconf = MagicMock()
    discovered = _build_discovered("factory-firmware")
    monitor._importable._import_discovery = _make_import_discovery(
        {f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": discovered}
    )

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.server = "factory-firmware.local."
    fake_info.port = 8080
    monkeypatch.setattr(importable_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    monitor.revisit_importable("factory-firmware")

    emitted = callbacks.calls_for("on_importable_added")
    assert len(emitted) == 1
    assert emitted[0][1].web_url == "http://factory-firmware.local:8080"


def test_revisit_importable_skips_seed_when_url_already_set() -> None:
    """An existing ``_http_urls`` entry isn't re-seeded — idempotent.

    Drives the ``_http_urls.get(device_name)`` short-circuit by
    pre-populating the cache; the public ``revisit_importable``
    fires, the importable is re-emitted, and the URL stays the
    pre-populated one (no AsyncServiceInfo lookup at all).
    """
    monitor, callbacks = _make_monitor(devices=[])
    monitor._mdns._zeroconf = MagicMock()
    monitor.state.http_urls["factory-firmware"] = "http://factory-firmware.local"
    monitor._importable._import_discovery = _make_import_discovery(
        {f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("factory-firmware")}
    )

    monitor.revisit_importable("factory-firmware")

    emitted = callbacks.calls_for("on_importable_added")
    assert emitted[0][1].web_url == "http://factory-firmware.local"


def test_revisit_importable_skips_seed_when_zeroconf_down() -> None:
    """Pre-start monitor (zeroconf=None) silently bails out of the seed."""
    monitor, _callbacks = _make_monitor(devices=[])
    monitor._mdns._zeroconf = None
    monitor._importable._import_discovery = _make_import_discovery(
        {f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("factory-firmware")}
    )

    monitor.revisit_importable("factory-firmware")  # must not raise

    assert monitor.state.http_urls == {}


def test_revisit_importable_seeds_nothing_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache miss keeps ``_http_urls`` clean — no fallback URL recorded.

    ``load_from_cache`` False means zeroconf has no entry for this
    HTTP service. The seed path is purely opportunistic; on miss
    we leave it to the regular browser-callback path.
    """
    monitor, _callbacks = _make_monitor(devices=[])
    monitor._mdns._zeroconf = MagicMock()
    monitor._mdns._zeroconf.zeroconf = MagicMock()
    monitor._importable._import_discovery = _make_import_discovery(
        {f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("factory-firmware")}
    )

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    monkeypatch.setattr(importable_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    monitor.revisit_importable("factory-firmware")

    assert monitor.state.http_urls == {}


# ---------------------------------------------------------------------------
# Ping loop / sweep — driven via start() with patched asyncio.sleep
# ---------------------------------------------------------------------------


def _shrink_ping_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the bootstrap delay + interval so ``_let_ping_loop_run_briefly`` sees sweeps."""
    monkeypatch.setattr(ping_module, "_PING_BOOTSTRAP_DELAY", 0)
    monkeypatch.setattr(ping_module, "_PING_INTERVAL", 0.001)


@pytest.mark.asyncio
async def test_start_drives_ping_pipeline_to_online_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start`` runs the bootstrap → resolve → sweep loop and flips devices ONLINE.

    Drives the public-API entry point (``start``) and lets the
    production ``_ping_loop`` task run. The bounded-sleep stub
    ends the loop after a couple of iterations so the test
    terminates; the device's state is the observable outcome.
    """
    device = _device(address="example.com", state=DeviceState.UNKNOWN)
    monitor, _callbacks = _make_monitor([device])

    async def _fake_ping(_target: str, **_kw: Any) -> Any:
        return MagicMock(is_alive=True)

    monkeypatch.setattr(ping_module, "icmp_ping", _fake_ping)
    monitor.state.dns_cache.async_resolve = AsyncMock(return_value=["192.0.2.5"])
    monitor.state.dns_cache.has_cached_failure = MagicMock(return_value=False)
    _shrink_ping_intervals(monkeypatch)

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _let_ping_loop_run_briefly(monitor)
        assert device.state == DeviceState.ONLINE
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_start_with_icmplib_unavailable_skips_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``icmp_ping is None`` (icmplib not installed) → sweep returns immediately.

    Drives via the public ``start``: the bootstrap sleep elapses,
    the loop reaches ``_ping_sweep``, the sweep sees no icmp
    primitive, and bails before touching the DNS cache. Pinning
    the negative — DNS cache untouched — anchors the early-return.
    """
    monitor, _callbacks = _make_monitor()

    monkeypatch.setattr(ping_module, "icmp_ping", None)
    monitor.state.dns_cache.async_resolve = AsyncMock()
    _shrink_ping_intervals(monkeypatch)

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _let_ping_loop_run_briefly(monitor)
        monitor.state.dns_cache.async_resolve.assert_not_called()
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_start_marks_offline_on_icmp_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised exception from ``icmp_ping`` flips the device OFFLINE.

    Production trigger: a ``.local`` host on a system without
    Avahi / mdnsd. icmplib's resolver raises ``NameLookupError``;
    the helper has to treat it as "unreachable" rather than bubble
    up and crash the sweep.
    """
    # Use a non-``.local`` address so ``_select_ping_targets`` skips
    # the mDNS cache lookup and falls through to the icmp probe —
    # which is what we want this test to exercise.
    device = _device(address="example.com", state=DeviceState.UNKNOWN)
    monitor, _callbacks = _make_monitor([device])

    async def _boom(*_a: Any, **_kw: Any) -> None:
        raise OSError("name lookup failed")

    monkeypatch.setattr(ping_module, "icmp_ping", _boom)
    monitor.state.dns_cache.async_resolve = AsyncMock(return_value=["10.0.0.1"])
    monitor.state.dns_cache.has_cached_failure = MagicMock(return_value=False)
    _shrink_ping_intervals(monkeypatch)

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _let_ping_loop_run_briefly(monitor)
        assert device.state == DeviceState.OFFLINE
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_start_skips_ping_for_cached_dns_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cached DNS failure flips the device OFFLINE without an icmp probe.

    Pin the ``_select_ping_targets`` skip + the debug log — both
    drive through the public ping pipeline via ``start``.
    """
    device = _device(address="example.com")
    monitor, _callbacks = _make_monitor([device])

    icmp_called: list[str] = []

    async def _icmp(target: str, **_kw: Any) -> Any:
        icmp_called.append(target)
        return MagicMock(is_alive=True)

    monkeypatch.setattr(ping_module, "icmp_ping", _icmp)
    monitor.state.dns_cache.has_cached_failure = MagicMock(return_value=True)
    _shrink_ping_intervals(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=ping_module.__name__):
        await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
        try:
            await _let_ping_loop_run_briefly(monitor)
        finally:
            await _stop_and_drain(monitor)

    assert device.state == DeviceState.OFFLINE
    assert icmp_called == []
    assert any("cached DNS failure" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_start_logs_ping_count_at_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Happy-path sweep emits the "Pinging N devices" debug message."""
    device = _device(address="example.com", state=DeviceState.UNKNOWN)
    monitor, _callbacks = _make_monitor([device])

    async def _icmp(_target: str, **_kw: Any) -> Any:
        return MagicMock(is_alive=True)

    monkeypatch.setattr(ping_module, "icmp_ping", _icmp)
    monitor.state.dns_cache.async_resolve = AsyncMock(return_value=["192.0.2.5"])
    monitor.state.dns_cache.has_cached_failure = MagicMock(return_value=False)
    _shrink_ping_intervals(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=ping_module.__name__):
        await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
        try:
            await _let_ping_loop_run_briefly(monitor)
        finally:
            await _stop_and_drain(monitor)

    assert any("Pinging 1 devices" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_repeat_sweep_with_unchanged_targets_logs_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``Pinging N devices`` only fires once when the target set is stable.

    Without this dedup the line re-emits every ``_PING_INTERVAL``
    forever on a steady fleet, spamming DEBUG-enabled logs with
    identical content. New devices, mDNS claims, or removals
    re-surface the line.
    """
    device = _device(address="example.com", state=DeviceState.UNKNOWN)
    monitor, _callbacks = _make_monitor([device])

    async def _icmp(_target: str, **_kw: Any) -> Any:
        return MagicMock(is_alive=True)

    monkeypatch.setattr(ping_module, "icmp_ping", _icmp)
    monitor.state.dns_cache.async_resolve = AsyncMock(return_value=["192.0.2.5"])
    monitor.state.dns_cache.has_cached_failure = MagicMock(return_value=False)
    # The shrunk interval gives the loop plenty of room to run
    # several sweeps inside ``_let_ping_loop_run_briefly``'s window — a
    # regression that re-logs every cycle would emit multiple
    # "Pinging" lines instead of one.
    _shrink_ping_intervals(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=ping_module.__name__):
        await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
        try:
            await _let_ping_loop_run_briefly(monitor)
        finally:
            await _stop_and_drain(monitor)

    ping_logs = [r for r in caplog.records if "Pinging" in r.message]
    assert len(ping_logs) == 1


@pytest.mark.asyncio
async def test_dns_failure_flicker_does_not_re_emit_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A device flipping between pingable and dns-failed each sweep logs once."""
    flaky = _device(name="zom", address="zom.local", state=DeviceState.UNKNOWN)
    stable = _device(name="ok", address="ok.example.com", state=DeviceState.UNKNOWN)
    monitor, _callbacks = _make_monitor([flaky, stable])

    async def _icmp(_target: str, **_kw: Any) -> Any:
        return MagicMock(is_alive=True, min_rtt=1.0)

    monkeypatch.setattr(ping_module, "icmp_ping", _icmp)
    monitor.state.dns_cache.async_resolve = AsyncMock(return_value=["192.0.2.5"])
    monitor.get_cached_addresses = MagicMock(return_value=None)
    cache_calls = {"n": 0}

    def _has_cached_failure(host: str) -> bool:
        if host != "zom.local":
            return False
        cache_calls["n"] += 1
        return cache_calls["n"] % 2 == 0

    monitor.state.dns_cache.has_cached_failure = MagicMock(side_effect=_has_cached_failure)
    _shrink_ping_intervals(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=ping_module.__name__):
        await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
        try:
            await _let_ping_loop_run_briefly(monitor)
        finally:
            await _stop_and_drain(monitor)

    membership_logs = [
        r for r in caplog.records if "Pinging" in r.message or "Skipping" in r.message
    ]
    assert len(membership_logs) == 1
    assert cache_calls["n"] >= 2


@pytest.mark.asyncio
async def test_start_skips_devices_without_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device with empty ``address`` is skipped — nothing to resolve.

    Pin the ``not device.address`` continue inside
    ``_select_ping_targets``. Driving via ``start`` keeps the
    test on the public-API side.
    """
    no_addr = _device(name="orphan", address="")
    monitor, _callbacks = _make_monitor([no_addr])
    monitor.state.dns_cache.async_resolve = AsyncMock(return_value=[])

    async def _icmp(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("icmp_ping must not be called")

    monkeypatch.setattr(ping_module, "icmp_ping", _icmp)
    _shrink_ping_intervals(monkeypatch)

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _let_ping_loop_run_briefly(monitor)
        monitor.state.dns_cache.async_resolve.assert_not_called()
    finally:
        await _stop_and_drain(monitor)


# ---------------------------------------------------------------------------
# Public read helpers — get_cached_addresses, apply_ip dedupe
# ---------------------------------------------------------------------------


def test_get_cached_addresses_returns_addresses_on_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache hit returns the parsed addresses; miss returns None."""
    monitor, _callbacks = _make_monitor()
    monitor._mdns._zeroconf = MagicMock()
    monitor._mdns._zeroconf.zeroconf = MagicMock()

    info = MagicMock()
    info.load_from_cache.return_value = True
    info.parsed_scoped_addresses.return_value = ["10.0.0.1", "10.0.0.2"]
    monkeypatch.setattr(mdns_module, "AddressResolver", lambda _name: info)

    assert monitor.get_cached_addresses("kitchen.local") == ["10.0.0.1", "10.0.0.2"]


def test_get_cached_addresses_returns_none_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``load_from_cache`` False → ``None`` (caller falls back to DNS / mDNS query)."""
    monitor, _callbacks = _make_monitor()
    monitor._mdns._zeroconf = MagicMock()
    monitor._mdns._zeroconf.zeroconf = MagicMock()

    info = MagicMock()
    info.load_from_cache.return_value = False
    monkeypatch.setattr(mdns_module, "AddressResolver", lambda _name: info)

    assert monitor.get_cached_addresses("kitchen.local") is None


def test_get_cached_addresses_returns_none_when_addresses_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty list (cache hit but expired / cleared) collapses to ``None``.

    Distinguishes from the load-miss branch above: there
    ``load_from_cache`` returned False; here it returned True but
    ``parsed_scoped_addresses`` came up empty. Both surface to the
    caller as "no addresses I can use".
    """
    monitor, _callbacks = _make_monitor()
    monitor._mdns._zeroconf = MagicMock()
    monitor._mdns._zeroconf.zeroconf = MagicMock()

    info = MagicMock()
    info.load_from_cache.return_value = True
    info.parsed_scoped_addresses.return_value = []
    monkeypatch.setattr(mdns_module, "AddressResolver", lambda _name: info)

    assert monitor.get_cached_addresses("kitchen.local") is None


@pytest.mark.asyncio
async def test_start_uses_v6_fallback_when_only_v6_in_mdns_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``.local`` device whose mDNS cache only carries V6 still gets an IP.

    Drives the V4-preference helper's fallback branch through the
    public ping pipeline: the device is ``.local``, the zeroconf
    cache resolves it but only to a scoped V6 address, and the
    monitor still claims it ONLINE under the mDNS source and pushes
    the V6 address into ``Device.ip``. Without the fallback the
    device would get ``apply_ip("")`` and the dashboard wouldn't
    have an IP to OTA against.
    """
    device = _device(address="kitchen.local", state=DeviceState.UNKNOWN)
    monitor, _callbacks = _make_monitor([device])

    cached_info = MagicMock()
    cached_info.load_from_cache.return_value = True
    cached_info.parsed_scoped_addresses.return_value = ["fe80::1%en0"]
    monkeypatch.setattr(mdns_module, "AddressResolver", lambda _name: cached_info)

    async def _icmp(*_a: Any, **_kw: Any) -> Any:
        # Should not be reached — the cached-addresses path
        # claims ONLINE and skips the icmp probe.
        raise AssertionError("icmp_ping must not be called for cached-mdns devices")

    monkeypatch.setattr(ping_module, "icmp_ping", _icmp)
    _shrink_ping_intervals(monkeypatch)

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _let_ping_loop_run_briefly(monitor)
        assert device.state == DeviceState.ONLINE
        assert device.ip == "fe80::1%en0"
    finally:
        await _stop_and_drain(monitor)


def test_apply_ip_short_circuits_when_value_unchanged() -> None:
    """``apply_ip`` is a no-op when both primary + list already match.

    Pin the dedupe so a refactor that drops the device-state
    comparison surfaces here — without it, every mDNS announcement
    would re-fire DEVICE_UPDATED for the same IP and the UI would
    thrash.
    """
    device = _device(ip="10.0.0.1", ip_addresses=["10.0.0.1"])
    monitor, callbacks = _make_monitor([device])

    monitor.apply_ip("kitchen", "10.0.0.1")

    assert callbacks.calls_for("on_ip_change") == []


def test_apply_ip_addresses_fires_when_list_changes_but_primary_does_not() -> None:
    """A device picking up a V6 address while keeping its V4 still fires.

    The dedupe has to look at both ``ip`` and ``ip_addresses`` so a
    dual-stack device whose V4 was already known surfaces its
    newly-announced V6 too.
    """
    device = _device(ip="10.0.0.1", ip_addresses=["10.0.0.1"])
    monitor, callbacks = _make_monitor([device])

    monitor.apply_ip_addresses("kitchen", ["10.0.0.1", "fe80::1%en0"])

    assert callbacks.calls_for("on_ip_change") == [
        ("on_ip_change", "kitchen", "10.0.0.1", ["10.0.0.1", "fe80::1%en0"]),
    ]
    assert device.ip == "10.0.0.1"
    assert device.ip_addresses == ["10.0.0.1", "fe80::1%en0"]


def test_apply_ip_preserves_multi_ip_list_when_primary_already_known() -> None:
    """Single-IP sources don't shrink a multi-IP view they don't see.

    MQTT discovery fires every ping interval against a device whose
    mDNS bundle (V4 + V6) already populated ``ip_addresses``. Without
    this guard, MQTT's narrower observation would collapse the list
    back to ``[v4]`` every cycle and re-hide the V6 from the
    dashboard until the next mDNS pass.
    """
    device = _device(ip="10.0.0.1", ip_addresses=["10.0.0.1", "fe80::1%en0"])
    monitor, callbacks = _make_monitor([device])

    monitor.apply_ip("kitchen", "10.0.0.1")

    assert callbacks.calls_for("on_ip_change") == []
    assert device.ip == "10.0.0.1"
    assert device.ip_addresses == ["10.0.0.1", "fe80::1%en0"]


def test_apply_ip_replaces_list_when_primary_is_new() -> None:
    """A different single IP overrides the list — the device moved networks.

    The previous V4 isn't in the new MQTT-reported set, so we treat
    that as authoritative for the live IP and let the next mDNS pass
    repopulate the V6 entries.
    """
    device = _device(ip="10.0.0.1", ip_addresses=["10.0.0.1", "fe80::1%en0"])
    monitor, callbacks = _make_monitor([device])

    monitor.apply_ip("kitchen", "10.0.0.99")

    assert callbacks.calls_for("on_ip_change") == [
        ("on_ip_change", "kitchen", "10.0.0.99", ["10.0.0.99"]),
    ]
    assert device.ip == "10.0.0.99"
    assert device.ip_addresses == ["10.0.0.99"]
