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

import esphome_device_builder.controllers._device_state_monitor as state_monitor_module
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, DeviceState

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


def _make_monitor(devices: list[Device] | None = None) -> DeviceStateMonitor:
    """Build a monitor shell with the apply-* callbacks recorded.

    Bypasses ``__init__`` so tests don't have to feed the full
    callback set or hit the real zeroconf bring-up. The callbacks
    mirror what the production controller does — write the new
    value back onto every matching Device — so the monitor's own
    dedupe (``_any_matching_device_differs``) sees an updated state
    on the second call instead of repeating itself.
    """
    devices = list(devices) if devices is not None else [_device()]
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)
    monitor._get_devices = lambda: devices
    monitor._get_devices_by_name = lambda name: [d for d in devices if d.name == name]
    monitor._is_ignored = lambda _name: False
    monitor._state_source = {}
    monitor._http_urls = {}
    monitor._zeroconf = None
    monitor._mdns_browser = None
    monitor._ping_task = None
    monitor._tasks = set()
    monitor._import_discovery = None

    # Each ``on_*_change`` callback writes the broadcast value back
    # onto every matching Device. ``_flip`` parameterises that over
    # the field name; the trailing ``*_extra`` swallows the third
    # ``source`` argument that ``on_state_change`` carries (the rest
    # are ``(name, value)`` pairs).
    def _flip(attr: str) -> Any:
        def _impl(name: str, value: Any, *_extra: Any) -> None:
            for d in devices:
                if d.name == name:
                    setattr(d, attr, value)

        return _impl

    monitor._on_state_change = MagicMock(side_effect=_flip("state"))
    monitor._on_ip_change = MagicMock(side_effect=_flip("ip"))
    monitor._on_version_change = MagicMock(side_effect=_flip("deployed_version"))
    monitor._on_config_hash_change = MagicMock(side_effect=_flip("deployed_config_hash"))
    monitor._on_api_encryption_change = MagicMock(side_effect=_flip("api_encryption_active"))
    monitor._on_importable_added = MagicMock()
    monitor._on_importable_removed = MagicMock()
    monitor._dns_cache = MagicMock()
    return monitor


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
    monkeypatch.setattr(state_monitor_module, "AsyncEsphomeZeroconf", lambda: fake_zeroconf)
    monkeypatch.setattr(
        state_monitor_module,
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

    monkeypatch.setattr(state_monitor_module, "AsyncServiceBrowser", _capture)

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


async def _drain_ping_task(monitor: DeviceStateMonitor) -> None:
    """Await the ping task to completion (typically via the bounded-sleep CancelledError).

    Ping-pipeline tests patch ``state_monitor_module.asyncio.sleep``
    to raise ``CancelledError`` after a few iterations; that
    propagates out of ``_ping_loop`` and ends the task. ``stop``
    would also cancel the task on teardown but draining first
    lets the test assert on the post-iteration state.
    """
    if monitor._ping_task is not None:
        await asyncio.gather(monitor._ping_task, return_exceptions=True)


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
    monitor = _make_monitor()
    await _start_with_captured_dispatch(monitor, monkeypatch)

    # Add a fake in-flight resolve task so stop has something to drain.
    async def _long_running() -> None:
        await asyncio.sleep(60)

    in_flight = asyncio.create_task(_long_running())
    monitor._tasks.add(in_flight)
    # Replace the zeroconf with one that has an awaitable async_close.
    monitor._zeroconf = MagicMock()
    monitor._zeroconf.async_close = AsyncMock()

    await _stop_and_drain(monitor)

    assert monitor._ping_task is None
    assert monitor._mdns_browser is None
    assert monitor._zeroconf is None
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
    monitor = _make_monitor()
    await _start_with_captured_dispatch(monitor, monkeypatch)
    monitor._mdns_browser.async_cancel = AsyncMock(side_effect=RuntimeError("browser broke"))
    monitor._zeroconf.async_close = AsyncMock()

    await _stop_and_drain(monitor)  # must not raise

    assert monitor._mdns_browser is None


@pytest.mark.asyncio
async def test_stop_swallows_zeroconf_close_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``zeroconf.async_close`` raise also lands at debug, not the caller."""
    monitor = _make_monitor()
    await _start_with_captured_dispatch(monitor, monkeypatch)
    monitor._zeroconf.async_close = AsyncMock(side_effect=RuntimeError("zeroconf broke"))

    await _stop_and_drain(monitor)  # must not raise

    assert monitor._zeroconf is None


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
    monitor = _make_monitor()

    def _boom() -> None:
        raise RuntimeError("no zeroconf for you")

    monkeypatch.setattr(state_monitor_module, "AsyncEsphomeZeroconf", _boom)

    async def _park() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(monitor, "_ping_loop", _park, raising=False)

    await monitor.start()
    try:
        assert monitor._zeroconf is None
        assert monitor._mdns_browser is None
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
    monitor = _make_monitor()
    fake_zeroconf = MagicMock()
    fake_zeroconf.zeroconf = MagicMock()
    fake_zeroconf.async_close = AsyncMock()
    monkeypatch.setattr(state_monitor_module, "AsyncEsphomeZeroconf", lambda: fake_zeroconf)
    monkeypatch.setattr(
        state_monitor_module,
        "DashboardImportDiscovery",
        lambda _cb: MagicMock(),
    )

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("browser broke")

    monkeypatch.setattr(state_monitor_module, "AsyncServiceBrowser", _boom)

    async def _park() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(monitor, "_ping_loop", _park, raising=False)

    await monitor.start()
    try:
        assert monitor._zeroconf is fake_zeroconf
        assert monitor._mdns_browser is None
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
    monitor = _make_monitor([device])
    monitor._state_source["kitchen"] = "mdns"
    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Removed,
        )
        assert device.state == DeviceState.OFFLINE
        assert device.ip == ""
        assert "kitchen" not in monitor._state_source
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_cache_hit_propagates_full_txt_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-hit Added event applies IP + version + config_hash + api_encryption.

    Anchors the full bundle so a refactor that drops one of the
    apply-* calls (or reorders in a way that hides a missing one)
    surfaces here. Also the V4 preference: apply_ip receives the
    V4 address even though the info also carries V6.
    """
    device = _device()
    monitor = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True

    def _addresses(mode: Any) -> list[str]:
        # IPVersion.V4Only first, then V6Only on the fallback call.
        return ["10.0.0.5"] if "V4" in repr(mode) else ["fe80::1%en0"]

    fake_info.parsed_scoped_addresses = _addresses
    fake_info.decoded_properties = {
        "version": "2026.5.0",
        "config_hash": "abcd1234",
        "api_encryption": "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
    }
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )

        assert device.state == DeviceState.ONLINE
        assert device.ip == "10.0.0.5"
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
    """No V4 in the cached info → ``apply_ip`` gets the first scoped V6.

    Pin the ``parsed_scoped_addresses(V4Only) or
    parsed_scoped_addresses(V6Only)`` short-circuit. Empty V4 list
    falls through; if the second call also returned empty, no
    apply_ip happens.
    """
    device = _device(state=DeviceState.UNKNOWN)
    monitor = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True

    def _addresses(mode: Any) -> list[str]:
        return [] if "V4" in repr(mode) else ["fe80::1%en0"]

    fake_info.parsed_scoped_addresses = _addresses
    fake_info.decoded_properties = {}
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert device.ip == "fe80::1%en0"
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_added_with_empty_api_encryption_pushes_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing api_encryption TXT means plaintext; pushed as empty string.

    The empty-string-vs-None distinction matters: ``None`` means
    "never seen", ``""`` means "service seen, no encryption
    advertised → confirmed plaintext". The frontend keys colour-
    coding off this tri-state.
    """
    device = _device(api_encryption_active=None)
    monitor = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.parsed_scoped_addresses = lambda _mode: []
    fake_info.decoded_properties = {}  # no api_encryption key at all
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"kitchen.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert device.api_encryption_active == ""
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
    monitor = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    fake_info.async_request = AsyncMock(return_value=True)
    fake_info.parsed_scoped_addresses = lambda _mode: ["10.0.0.5"]
    fake_info.decoded_properties = {"version": "2026.5.0"}
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
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
    monitor = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    fake_info.async_request = AsyncMock(return_value=False)
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
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
    monitor = _make_monitor([device])

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    fake_info.async_request = AsyncMock(side_effect=OSError("network flap"))
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
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
    monitor = _make_monitor()  # only "kitchen" configured
    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            ESPHOMELIB_SERVICE_TYPE,
            f"stranger.{ESPHOMELIB_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert monitor._on_state_change.call_count == 0
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
    monitor = _make_monitor(devices=[])
    discovered = _build_discovered("factory-firmware")
    discovery = _make_import_discovery({f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": discovered})

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.server = "factory-firmware.local."
    fake_info.port = 8080
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"factory-firmware.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert monitor._http_urls["factory-firmware"] == ("http://factory-firmware.local:8080")
        # The importable was re-emitted with the new web_url.
        emitted = monitor._on_importable_added.call_args_list
        assert any(call.args[0].web_url for call in emitted)
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
    monitor = _make_monitor(devices=[])
    discovery = _make_import_discovery()  # no discoveries

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.server = "stranger.local."
    fake_info.port = 80
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"stranger.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert monitor._http_urls == {}
        monitor._on_importable_added.assert_not_called()
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
    monitor = _make_monitor(devices=[])
    discovered = _build_discovered("factory-firmware")
    discovery = _make_import_discovery({f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": discovered})
    monitor._http_urls["factory-firmware"] = "http://factory-firmware.local"

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"factory-firmware.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Removed,
        )
        assert "factory-firmware" not in monitor._http_urls
        monitor._on_importable_added.assert_called()
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_http_service_removed_for_untracked_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing an HTTP service we never tracked → no fan-out at all."""
    monitor = _make_monitor(devices=[])
    discovery = _make_import_discovery()

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"stranger.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Removed,
        )
        monitor._on_importable_added.assert_not_called()
    finally:
        await _stop_and_drain(monitor)


@pytest.mark.asyncio
async def test_dispatch_http_service_added_cache_miss_resolves_and_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache-miss HTTP service spawns a resolve task that fills the URL on success."""
    monitor = _make_monitor(devices=[])
    discovered = _build_discovered("factory-firmware")
    discovery = _make_import_discovery({f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": discovered})

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    fake_info.async_request = AsyncMock(return_value=True)
    fake_info.server = "factory-firmware.local."
    fake_info.port = 80
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    dispatch = await _start_with_captured_dispatch(monitor, monkeypatch, import_discovery=discovery)
    try:
        dispatch(
            monitor._zeroconf.zeroconf,
            HTTP_SERVICE_TYPE,
            f"factory-firmware.{HTTP_SERVICE_TYPE}",
            ServiceStateChange.Added,
        )
        assert len(monitor._tasks) == 1
        await asyncio.gather(*list(monitor._tasks))
        assert monitor._http_urls["factory-firmware"] == "http://factory-firmware.local"
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
    monitor = _make_monitor()
    monitor._import_discovery = None

    monitor.revisit_all_importables()  # must not raise

    monitor._on_importable_added.assert_not_called()


def test_revisit_all_importables_re_emits_every_cached_entry() -> None:
    """``revisit_all_importables`` walks every entry and re-fires the ADD callback."""
    monitor = _make_monitor(devices=[])
    monitor._import_discovery = _make_import_discovery(
        {
            f"a.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("a"),
            f"b.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("b"),
        }
    )

    monitor.revisit_all_importables()

    emitted = {call.args[0].name for call in monitor._on_importable_added.call_args_list}
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
    monitor = _make_monitor(devices=[])
    monitor._zeroconf = MagicMock()
    monitor._zeroconf.zeroconf = MagicMock()
    discovered = _build_discovered("factory-firmware")
    monitor._import_discovery = _make_import_discovery(
        {f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": discovered}
    )

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    fake_info.server = "factory-firmware.local."
    fake_info.port = 8080
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    monitor.revisit_importable("factory-firmware")

    emitted = monitor._on_importable_added.call_args_list
    assert len(emitted) == 1
    assert emitted[0].args[0].web_url == "http://factory-firmware.local:8080"


def test_revisit_importable_skips_seed_when_url_already_set() -> None:
    """An existing ``_http_urls`` entry isn't re-seeded — idempotent.

    Drives the ``_http_urls.get(device_name)`` short-circuit by
    pre-populating the cache; the public ``revisit_importable``
    fires, the importable is re-emitted, and the URL stays the
    pre-populated one (no AsyncServiceInfo lookup at all).
    """
    monitor = _make_monitor(devices=[])
    monitor._zeroconf = MagicMock()
    monitor._http_urls["factory-firmware"] = "http://factory-firmware.local"
    monitor._import_discovery = _make_import_discovery(
        {f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("factory-firmware")}
    )

    monitor.revisit_importable("factory-firmware")

    emitted = monitor._on_importable_added.call_args_list
    assert emitted[0].args[0].web_url == "http://factory-firmware.local"


def test_revisit_importable_skips_seed_when_zeroconf_down() -> None:
    """Pre-start monitor (zeroconf=None) silently bails out of the seed."""
    monitor = _make_monitor(devices=[])
    monitor._zeroconf = None
    monitor._import_discovery = _make_import_discovery(
        {f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("factory-firmware")}
    )

    monitor.revisit_importable("factory-firmware")  # must not raise

    assert monitor._http_urls == {}


def test_revisit_importable_seeds_nothing_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache miss keeps ``_http_urls`` clean — no fallback URL recorded.

    ``load_from_cache`` False means zeroconf has no entry for this
    HTTP service. The seed path is purely opportunistic; on miss
    we leave it to the regular browser-callback path.
    """
    monitor = _make_monitor(devices=[])
    monitor._zeroconf = MagicMock()
    monitor._zeroconf.zeroconf = MagicMock()
    monitor._import_discovery = _make_import_discovery(
        {f"factory-firmware.{ESPHOMELIB_SERVICE_TYPE}": _build_discovered("factory-firmware")}
    )

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    monkeypatch.setattr(state_monitor_module, "AsyncServiceInfo", lambda *_a, **_kw: fake_info)

    monitor.revisit_importable("factory-firmware")

    assert monitor._http_urls == {}


# ---------------------------------------------------------------------------
# Ping loop / sweep — driven via start() with patched asyncio.sleep
# ---------------------------------------------------------------------------


def _bounded_sleep_factory(max_iterations: int = 3) -> Any:
    """Build an ``asyncio.sleep`` replacement that bails after *max_iterations*.

    ``_ping_loop`` is an infinite ``while True: await asyncio.sleep(_PING_INTERVAL)``
    loop, so a test that drives it via ``start()`` needs a way to
    end. Patching the module-local ``asyncio.sleep`` to raise
    ``CancelledError`` after a few iterations terminates the task
    cleanly without the test having to call ``cancel`` itself.
    """
    iterations = [0]

    async def _impl(_seconds: float) -> None:
        iterations[0] += 1
        if iterations[0] >= max_iterations:
            raise asyncio.CancelledError

    return _impl


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
    monitor = _make_monitor([device])

    async def _fake_ping(_target: str, **_kw: Any) -> Any:
        return MagicMock(is_alive=True)

    monkeypatch.setattr(state_monitor_module, "icmp_ping", _fake_ping)
    monitor._dns_cache.async_resolve = AsyncMock(return_value=["192.0.2.5"])
    monitor._dns_cache.has_cached_failure = MagicMock(return_value=False)
    monkeypatch.setattr(state_monitor_module.asyncio, "sleep", _bounded_sleep_factory())

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _drain_ping_task(monitor)
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
    monitor = _make_monitor()

    monkeypatch.setattr(state_monitor_module, "icmp_ping", None)
    monitor._dns_cache.async_resolve = AsyncMock()
    monkeypatch.setattr(state_monitor_module.asyncio, "sleep", _bounded_sleep_factory(2))

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _drain_ping_task(monitor)
        monitor._dns_cache.async_resolve.assert_not_called()
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
    monitor = _make_monitor([device])

    async def _boom(*_a: Any, **_kw: Any) -> None:
        raise OSError("name lookup failed")

    monkeypatch.setattr(state_monitor_module, "icmp_ping", _boom)
    monitor._dns_cache.async_resolve = AsyncMock(return_value=["10.0.0.1"])
    monitor._dns_cache.has_cached_failure = MagicMock(return_value=False)
    monkeypatch.setattr(state_monitor_module.asyncio, "sleep", _bounded_sleep_factory(2))

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _drain_ping_task(monitor)
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
    monitor = _make_monitor([device])

    icmp_called: list[str] = []

    async def _icmp(target: str, **_kw: Any) -> Any:
        icmp_called.append(target)
        return MagicMock(is_alive=True)

    monkeypatch.setattr(state_monitor_module, "icmp_ping", _icmp)
    monitor._dns_cache.has_cached_failure = MagicMock(return_value=True)
    monkeypatch.setattr(state_monitor_module.asyncio, "sleep", _bounded_sleep_factory(2))

    with caplog.at_level(logging.DEBUG, logger=state_monitor_module.__name__):
        await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
        try:
            await _drain_ping_task(monitor)
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
    monitor = _make_monitor([device])

    async def _icmp(_target: str, **_kw: Any) -> Any:
        return MagicMock(is_alive=True)

    monkeypatch.setattr(state_monitor_module, "icmp_ping", _icmp)
    monitor._dns_cache.async_resolve = AsyncMock(return_value=["192.0.2.5"])
    monitor._dns_cache.has_cached_failure = MagicMock(return_value=False)
    monkeypatch.setattr(state_monitor_module.asyncio, "sleep", _bounded_sleep_factory(2))

    with caplog.at_level(logging.DEBUG, logger=state_monitor_module.__name__):
        await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
        try:
            await _drain_ping_task(monitor)
        finally:
            await _stop_and_drain(monitor)

    assert any("Pinging 1 devices" in rec.message for rec in caplog.records)


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
    monitor = _make_monitor([no_addr])
    monitor._dns_cache.async_resolve = AsyncMock(return_value=[])

    async def _icmp(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("icmp_ping must not be called")

    monkeypatch.setattr(state_monitor_module, "icmp_ping", _icmp)
    monkeypatch.setattr(state_monitor_module.asyncio, "sleep", _bounded_sleep_factory(2))

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _drain_ping_task(monitor)
        monitor._dns_cache.async_resolve.assert_not_called()
    finally:
        await _stop_and_drain(monitor)


# ---------------------------------------------------------------------------
# Public read helpers — get_cached_addresses, apply_ip dedupe
# ---------------------------------------------------------------------------


def test_get_cached_addresses_returns_addresses_on_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache hit returns the parsed addresses; miss returns None."""
    monitor = _make_monitor()
    monitor._zeroconf = MagicMock()
    monitor._zeroconf.zeroconf = MagicMock()

    info = MagicMock()
    info.load_from_cache.return_value = True
    info.parsed_scoped_addresses.return_value = ["10.0.0.1", "10.0.0.2"]
    monkeypatch.setattr(state_monitor_module, "AddressResolver", lambda _name: info)

    assert monitor.get_cached_addresses("kitchen.local") == ["10.0.0.1", "10.0.0.2"]


def test_get_cached_addresses_returns_none_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``load_from_cache`` False → ``None`` (caller falls back to DNS / mDNS query)."""
    monitor = _make_monitor()
    monitor._zeroconf = MagicMock()
    monitor._zeroconf.zeroconf = MagicMock()

    info = MagicMock()
    info.load_from_cache.return_value = False
    monkeypatch.setattr(state_monitor_module, "AddressResolver", lambda _name: info)

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
    monitor = _make_monitor()
    monitor._zeroconf = MagicMock()
    monitor._zeroconf.zeroconf = MagicMock()

    info = MagicMock()
    info.load_from_cache.return_value = True
    info.parsed_scoped_addresses.return_value = []
    monkeypatch.setattr(state_monitor_module, "AddressResolver", lambda _name: info)

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
    monitor = _make_monitor([device])

    cached_info = MagicMock()
    cached_info.load_from_cache.return_value = True
    cached_info.parsed_scoped_addresses.return_value = ["fe80::1%en0"]
    monkeypatch.setattr(state_monitor_module, "AddressResolver", lambda _name: cached_info)

    async def _icmp(*_a: Any, **_kw: Any) -> Any:
        # Should not be reached — the cached-addresses path
        # claims ONLINE and skips the icmp probe.
        raise AssertionError("icmp_ping must not be called for cached-mdns devices")

    monkeypatch.setattr(state_monitor_module, "icmp_ping", _icmp)
    monkeypatch.setattr(state_monitor_module.asyncio, "sleep", _bounded_sleep_factory(2))

    await _start_with_captured_dispatch(monitor, monkeypatch, park_ping_loop=False)
    try:
        await _drain_ping_task(monitor)
        assert device.state == DeviceState.ONLINE
        assert device.ip == "fe80::1%en0"
    finally:
        await _stop_and_drain(monitor)


def test_apply_ip_short_circuits_when_value_unchanged() -> None:
    """``apply_ip`` is a no-op when every matching device already has *ip*.

    Pin the dedupe so a refactor that drops the
    ``_any_matching_device_differs`` guard surfaces here — without
    it, every mDNS announcement would re-fire DEVICE_UPDATED for
    the same IP and the UI would thrash.
    """
    device = _device(ip="10.0.0.1")
    monitor = _make_monitor([device])

    monitor.apply_ip("kitchen", "10.0.0.1")

    monitor._on_ip_change.assert_not_called()
