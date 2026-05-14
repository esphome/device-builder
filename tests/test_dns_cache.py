"""Tests for the TTL'd DNS cache used by ping pre-resolution + OTA cache args."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers import (
    _dns_cache as dns_cache_mod,
)
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.controllers._device_state_monitor import (
    controller as sm,
)
from esphome_device_builder.controllers._dns_cache import DNSCache
from esphome_device_builder.controllers.config import (
    get_board_id,
    get_device_ip,
    set_device_metadata,
)
from esphome_device_builder.helpers import device_yaml
from esphome_device_builder.models import Device, DeviceState

from .conftest import make_devices_controller_with_bus


@pytest.fixture
def fake_resolver() -> Callable[..., Callable[[str], Awaitable[list[str]]]]:
    """Build a stub for ``icmplib.async_resolve``.

    The factory captures call counts and returns successive results
    from *responses* — a list whose entries are either a list of IPs
    or an exception class to raise.
    """

    def factory(
        *responses: list[str] | type[BaseException],
    ) -> Callable[[str], Awaitable[list[str]]]:
        calls: list[str] = []
        queue = list(responses)

        async def stub(name: str, family: int | None = None) -> list[str]:
            calls.append(name)
            if not queue:
                msg = "no more queued responses"
                raise RuntimeError(msg)
            response = queue.pop(0)
            if isinstance(response, list):
                return response
            raise response(name)

        stub.calls = calls  # type: ignore[attr-defined]
        return stub

    return factory


# ----------------------------------------------------------------------
# get_cached_addresses
# ----------------------------------------------------------------------


def test_literal_ip_returns_self_without_cache() -> None:
    """Literal IPv4/IPv6 addresses short-circuit and skip the cache."""
    cache = DNSCache()
    assert cache.get_cached_addresses("10.0.0.1") == ["10.0.0.1"]
    assert cache.get_cached_addresses("fe80::1") == ["fe80::1"]
    assert cache._cache == {}


def test_get_cached_returns_none_on_miss() -> None:
    assert DNSCache().get_cached_addresses("esp.example.com") is None


def test_get_cached_returns_none_after_ttl_expiry() -> None:
    cache = DNSCache(ttl=60)
    cache._cache["esp.example.com"] = (time.monotonic() - 1, ["10.0.0.1"])
    assert cache.get_cached_addresses("esp.example.com") is None


def test_get_cached_normalises_hostname() -> None:
    """Trailing dot + uppercase resolve to the same cache key."""
    cache = DNSCache(ttl=60)
    cache._cache["esp.example.com"] = (time.monotonic() + 60, ["10.0.0.1"])
    assert cache.get_cached_addresses("ESP.example.com.") == ["10.0.0.1"]


def test_get_cached_hides_failed_resolutions() -> None:
    """A cached failure is treated as a miss so callers can fall back."""
    cache = DNSCache(ttl=60)
    cache._cache["esp.example.com"] = (time.monotonic() + 60, None)
    assert cache.get_cached_addresses("esp.example.com") is None


# ----------------------------------------------------------------------
# has_cached_failure
# ----------------------------------------------------------------------


def test_has_cached_failure_false_on_miss() -> None:
    assert DNSCache().has_cached_failure("esp.example.com") is False


def test_has_cached_failure_true_for_fresh_failure() -> None:
    cache = DNSCache(ttl=60)
    cache._cache["esp.example.com"] = (time.monotonic() + 60, None)
    assert cache.has_cached_failure("esp.example.com") is True


def test_has_cached_failure_false_after_ttl_expiry() -> None:
    cache = DNSCache(ttl=60)
    cache._cache["esp.example.com"] = (time.monotonic() - 1, None)
    assert cache.has_cached_failure("esp.example.com") is False


def test_has_cached_failure_false_for_successful_entry() -> None:
    cache = DNSCache(ttl=60)
    cache._cache["esp.example.com"] = (time.monotonic() + 60, ["10.0.0.1"])
    assert cache.has_cached_failure("esp.example.com") is False


def test_has_cached_failure_false_for_literal_ip() -> None:
    assert DNSCache().has_cached_failure("10.0.0.1") is False
    assert DNSCache().has_cached_failure("fe80::1") is False


def test_has_cached_failure_normalises_hostname() -> None:
    cache = DNSCache(ttl=60)
    cache._cache["esp.example.com"] = (time.monotonic() + 60, None)
    assert cache.has_cached_failure("ESP.example.com.") is True


# ----------------------------------------------------------------------
# async_resolve
# ----------------------------------------------------------------------


async def test_async_resolve_caches_first_call(fake_resolver) -> None:
    stub = fake_resolver(["10.0.0.1"])
    cache = DNSCache(ttl=60)
    with patch.object(dns_cache_mod, "async_resolve", stub):
        first = await cache.async_resolve("esp.example.com")
        second = await cache.async_resolve("esp.example.com")
    assert first == ["10.0.0.1"]
    assert second == ["10.0.0.1"]
    assert stub.calls == ["esp.example.com"]


async def test_async_resolve_re_resolves_after_ttl(fake_resolver) -> None:
    stub = fake_resolver(["10.0.0.1"], ["10.0.0.2"])
    cache = DNSCache(ttl=60)
    with patch.object(dns_cache_mod, "async_resolve", stub):
        first = await cache.async_resolve("esp.example.com")
        # Manually expire the entry instead of sleeping.
        expires_at, addresses = cache._cache["esp.example.com"]
        cache._cache["esp.example.com"] = (expires_at - 9999, addresses)
        second = await cache.async_resolve("esp.example.com")
    assert first == ["10.0.0.1"]
    assert second == ["10.0.0.2"]
    assert stub.calls == ["esp.example.com", "esp.example.com"]


async def test_async_resolve_caches_failures(fake_resolver) -> None:
    """
    A failed lookup is cached for the TTL.

    A single transient error must not turn into a re-resolution storm
    next cycle.
    """
    stub = fake_resolver(dns_cache_mod.NameLookupError)
    cache = DNSCache(ttl=60)
    with patch.object(dns_cache_mod, "async_resolve", stub):
        result = await cache.async_resolve("esp.example.com")
        # Second call should hit the cached failure and NOT re-resolve.
        result2 = await cache.async_resolve("esp.example.com")
    assert result is None
    assert result2 is None
    assert stub.calls == ["esp.example.com"]
    assert cache.get_cached_addresses("esp.example.com") is None


async def test_async_resolve_falls_back_to_bare_hostname(fake_resolver) -> None:
    """When ``foo.local`` resolution fails, retry the bare hostname."""
    stub = fake_resolver(dns_cache_mod.NameLookupError, ["10.0.0.1"])
    cache = DNSCache(ttl=60)
    with patch.object(dns_cache_mod, "async_resolve", stub):
        result = await cache.async_resolve("foo.local")
    assert result == ["10.0.0.1"]
    assert stub.calls == ["foo.local", "foo"]


async def test_async_resolve_returns_literal_ip_without_lookup(fake_resolver) -> None:
    stub = fake_resolver(["1.2.3.4"])
    cache = DNSCache(ttl=60)
    with patch.object(dns_cache_mod, "async_resolve", stub):
        result = await cache.async_resolve("10.0.0.1")
    assert result == ["10.0.0.1"]
    assert stub.calls == []


async def test_async_resolve_handles_timeout(fake_resolver) -> None:
    """A timeout during resolution is treated as a failure (None)."""
    stub = fake_resolver(TimeoutError)
    cache = DNSCache(ttl=60)
    with patch.object(dns_cache_mod, "async_resolve", stub):
        result = await cache.async_resolve("esp.example.com")
    assert result is None


# ----------------------------------------------------------------------
# DeviceStateMonitor — ping pre-resolution
# ----------------------------------------------------------------------


def _device(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "configuration": "kitchen.yaml",
        "address": "esp.example.com",
        "state": DeviceState.UNKNOWN,
    }
    base.update(overrides)
    return Device(**base)


async def test_ping_sweep_pre_resolves_via_dns_cache(fake_resolver) -> None:
    """A ping sweep populates the DNS cache and pings the resolved IP."""
    devices = [_device()]
    state_changes: list[tuple[str, object, str]] = []
    ip_changes: list[tuple[str, str, list[str]]] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda n, s, src: state_changes.append((n, s, src)),
        on_ip_change=lambda n, ip, addrs: ip_changes.append((n, ip, list(addrs))),
    )

    resolver = fake_resolver(["10.0.0.1"])
    pinged: list[str] = []

    async def fake_ping(target, **_kwargs):  # type: ignore[no-untyped-def]
        pinged.append(target)

        class _R:
            is_alive = True

        return _R()

    with (
        patch.object(dns_cache_mod, "async_resolve", resolver),
        patch.object(sm, "icmp_ping", fake_ping),
    ):
        await monitor._ping_sweep()

    assert pinged == ["10.0.0.1"]
    assert ip_changes == [("kitchen", "10.0.0.1", ["10.0.0.1"])]
    # DNS cache is now warm — ``get_cached_dns_addresses`` should hit
    # without triggering another resolver call.
    assert monitor.get_cached_dns_addresses("esp.example.com") == ["10.0.0.1"]


async def test_ping_sweep_applies_ip_for_local_hosts(fake_resolver) -> None:
    """``.local`` devices reachable only via ping get their resolved IP applied.

    Non-API ESPHome devices (no ``_esphomelib._tcp`` broadcast)
    only surface in the ping sweep. Without applying the
    DNS/mDNS-resolved address to ``device.ip`` the drawer / table
    would show an em-dash for the IP row even after successful
    pings — the
    ``zwave-proxy-seeedw5500.local``-shows-no-IP-while-ping-active
    bug. ``apply_ip``'s "preserve existing list when target is in
    it" branch keeps a richer multi-IP set populated by mDNS
    safe; this test pins the empty-existing case.
    """
    devices = [_device(address="kitchen.local")]
    ip_changes: list[tuple[str, str, list[str]]] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda *_: None,
        on_ip_change=lambda n, ip, addrs: ip_changes.append((n, ip, list(addrs))),
    )

    resolver = fake_resolver(["192.168.1.50"])

    async def fake_ping(_target, **_kwargs):  # type: ignore[no-untyped-def]
        class _R:
            is_alive = True

        return _R()

    with (
        patch.object(dns_cache_mod, "async_resolve", resolver),
        patch.object(sm, "icmp_ping", fake_ping),
    ):
        await monitor._ping_sweep()

    assert ip_changes == [("kitchen", "192.168.1.50", ["192.168.1.50"])]


async def test_ping_sweep_rescues_local_device_from_zeroconf_cache() -> None:
    """A ``.local`` device in zeroconf's cache short-circuits before ping.

    Catches the case where the ``AsyncServiceBrowser`` ``Added``
    callback didn't fire for us (multicast packet drop or startup race)
    but zeroconf's underlying cache has the entry from the periodic
    PTR queries. Without this rescue the ping sweep falls through to
    the bare-hostname DNS fallback, which can resolve to an
    unreachable IP on a different subnet and report a phantom OFFLINE
    for a device that's actually right there.
    """
    devices = [_device(address="winefridge.local", name="winefridge")]
    state_changes: list[tuple[str, object, str]] = []
    ip_changes: list[tuple[str, str, list[str]]] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda n, s, src: state_changes.append((n, s, src)),
        on_ip_change=lambda n, ip, addrs: ip_changes.append((n, ip, list(addrs))),
    )

    pinged: list[str] = []

    async def fake_ping(target, **_kwargs):  # type: ignore[no-untyped-def]
        pinged.append(target)

        class _R:
            is_alive = False

        return _R()

    with (
        patch.object(monitor, "get_cached_addresses", lambda host: ["192.168.213.11"]),
        patch.object(sm, "icmp_ping", fake_ping),
    ):
        await monitor._ping_sweep()

    assert pinged == []
    assert state_changes == [("winefridge", DeviceState.ONLINE, "mdns")]
    assert ip_changes == [("winefridge", "192.168.213.11", ["192.168.213.11"])]
    assert monitor.priority_for("winefridge") == "mdns"


async def test_ping_sweep_marks_offline_directly_on_dns_failure(fake_resolver) -> None:
    """DNS resolution failure → OFFLINE without calling icmp_ping.

    Handing the bare hostname to ``icmp_ping`` would re-resolve via
    icmplib's own resolver every sweep — bypassing our DNS cache and
    hammering the system resolver for nothing. Instead, treat a
    cache-confirmed lookup failure as the "we tried, can't reach"
    signal and apply OFFLINE directly.
    """
    devices = [_device()]
    state_changes: list[tuple[str, DeviceState, str]] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda n, s, src: state_changes.append((n, s, src)),
        on_ip_change=lambda *_: None,
    )

    resolver = fake_resolver(dns_cache_mod.NameLookupError)
    pinged: list[str] = []

    async def fake_ping(target, **_kwargs):  # type: ignore[no-untyped-def]
        pinged.append(target)

        class _R:
            is_alive = False

        return _R()

    with (
        patch.object(dns_cache_mod, "async_resolve", resolver),
        patch.object(sm, "icmp_ping", fake_ping),
    ):
        await monitor._ping_sweep()

    # Crucially: ``icmp_ping`` was NOT called. The resolution failure
    # was enough to declare the device offline.
    assert pinged == []
    assert state_changes == [("kitchen", DeviceState.OFFLINE, "ping")]


async def test_ping_sweep_skips_devices_with_cached_dns_failure(fake_resolver) -> None:
    """Pre-cached DNS failure → no resolver call, no icmp_ping, OFFLINE applied.

    Once a hostname has a cached failure entry from a previous sweep,
    subsequent sweeps must short-circuit before logging "Pinging N
    devices" — otherwise the log lists devices we already know we
    won't reach. The resolver call must also be skipped so we don't
    re-attempt a known-bad lookup every minute.
    """
    devices = [_device()]
    state_changes: list[tuple[str, DeviceState, str]] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda n, s, src: state_changes.append((n, s, src)),
        on_ip_change=lambda *_: None,
    )
    # Prime the cache with a fresh failure so the sweep should skip.
    monitor.state.dns_cache._cache["esp.example.com"] = (time.monotonic() + 60, None)

    resolver = fake_resolver(["10.0.0.1"])
    pinged: list[str] = []

    async def fake_ping(target, **_kwargs):  # type: ignore[no-untyped-def]
        pinged.append(target)

        class _R:
            is_alive = True

        return _R()

    with (
        patch.object(dns_cache_mod, "async_resolve", resolver),
        patch.object(sm, "icmp_ping", fake_ping),
    ):
        await monitor._ping_sweep()

    assert pinged == []
    assert resolver.calls == []
    assert state_changes == [("kitchen", DeviceState.OFFLINE, "ping")]


async def test_ping_marks_offline_when_icmp_raises(fake_resolver) -> None:
    """A ping that raises (NameLookupError, NoRouteToHost, …) flips the device OFFLINE.

    Previously these exceptions short-circuited ``_ping_device`` and
    left the device stuck in UNKNOWN forever — even after mDNS / MQTT
    had also failed to find it. The dashboard's red dot was
    unreachable. Now the failure mode is treated as "we tried, we
    couldn't reach it" and the state flips to OFFLINE; a subsequent
    successful ping flips it right back to ONLINE.
    """
    devices = [_device()]
    state_changes: list[tuple[str, DeviceState, str]] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda n, s, src: state_changes.append((n, s, src)),
        on_ip_change=lambda *_: None,
    )

    resolver = fake_resolver(dns_cache_mod.NameLookupError)

    async def raising_ping(_target, **_kwargs):  # type: ignore[no-untyped-def]
        raise dns_cache_mod.NameLookupError("not resolvable")

    with (
        patch.object(dns_cache_mod, "async_resolve", resolver),
        patch.object(sm, "icmp_ping", raising_ping),
    ):
        await monitor._ping_sweep()

    assert state_changes == [("kitchen", DeviceState.OFFLINE, "ping")]


# ----------------------------------------------------------------------
# IP persistence
# ----------------------------------------------------------------------


def test_set_device_ip_writes_to_metadata(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``set_device_metadata(ip=...)`` round-trips through ``get_device_ip``."""
    set_device_metadata(tmp_path, "kitchen.yaml", ip="10.0.0.1")
    assert get_device_ip(tmp_path, "kitchen.yaml") == "10.0.0.1"


def test_set_device_ip_preserves_existing_when_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """
    Empty/None ip leaves the persisted value alone.

    Protects the OTA cache during offline windows — mDNS clears the
    in-memory IP whenever a device drops off, but we still want the
    cache to be warm next time we OTA.
    """
    set_device_metadata(tmp_path, "kitchen.yaml", ip="10.0.0.1")
    set_device_metadata(tmp_path, "kitchen.yaml", ip="")
    set_device_metadata(tmp_path, "kitchen.yaml", ip=None)
    assert get_device_ip(tmp_path, "kitchen.yaml") == "10.0.0.1"


def test_set_device_ip_unrelated_field_does_not_clobber_ip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Setting ``board_id`` later doesn't drop the persisted ``ip`` field."""
    set_device_metadata(tmp_path, "kitchen.yaml", ip="10.0.0.1")
    set_device_metadata(tmp_path, "kitchen.yaml", board_id="esp32-devkit")
    assert get_device_ip(tmp_path, "kitchen.yaml") == "10.0.0.1"


def test_get_device_ip_returns_empty_for_unknown_device(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert get_device_ip(tmp_path, "kitchen.yaml") == ""


def test_load_device_from_storage_threads_ip_through(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Scanner-loaded devices carry the persisted IP into the model."""
    # ``ext_storage_path`` walks ``CORE.config_path`` which isn't set
    # in this test; redirect it to ``tmp_path`` and short-circuit
    # ``StorageJSON.load`` so we exercise just the YAML + ip plumbing.
    monkeypatch.setattr(
        device_yaml,
        "resolve_storage_path",
        lambda config: tmp_path / f"{config}.json",
    )
    monkeypatch.setattr(device_yaml.StorageJSON, "load", staticmethod(lambda _path: None))

    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n  friendly_name: Kitchen\n")

    device = device_yaml.load_device_from_storage(yaml_path, board_id="esp32-devkit", ip="10.0.0.1")
    assert device.ip == "10.0.0.1"
    assert device.board_id == "esp32-devkit"


def test_load_device_from_storage_address_falls_back_to_filename_local(  # type: ignore[no-untyped-def]
    monkeypatch, tmp_path
) -> None:
    """Never-compiled devices get ``<filename-stem>.local`` so the ping sweep includes them.

    Without this fallback, ``Device.address`` was empty for any
    device that hadn't been built yet, so the sweep filter
    (``if not device.address: continue``) excluded them and they
    stayed UNKNOWN forever — that's what the user reported with
    ``wr2-test`` and friends.
    """
    monkeypatch.setattr(
        device_yaml,
        "resolve_storage_path",
        lambda config: tmp_path / f"{config}.json",
    )
    monkeypatch.setattr(device_yaml.StorageJSON, "load", staticmethod(lambda _p: None))

    yaml_path = tmp_path / "wr2-test.yaml"
    yaml_path.write_text("esphome:\n  name: wr2-test\n")

    device = device_yaml.load_device_from_storage(yaml_path)
    assert device.address == "wr2-test.local"


def test_load_device_from_storage_address_uses_filename_not_parsed_name(  # type: ignore[no-untyped-def]
    monkeypatch, tmp_path
) -> None:
    """Address fallback uses the filename, not the YAML-parsed ``name``.

    Configs that pull the device name from a remote ``dashboard_import``
    package can leave ``parse_esphome_meta`` returning a stem-shaped
    package id (like ``ratgdo.esphome``) instead of the actual device
    name. Using ``<name>.local`` as the fallback would then claim
    ``ratgdo.esphome.local`` for a device whose YAML is
    ``largegarage.yaml`` — exactly the bug the user reported. The
    filename stem is canonical and matches what the user types.
    """
    monkeypatch.setattr(
        device_yaml,
        "resolve_storage_path",
        lambda config: tmp_path / f"{config}.json",
    )
    monkeypatch.setattr(device_yaml.StorageJSON, "load", staticmethod(lambda _p: None))

    # YAML where parse_esphome_meta will resolve the name to whatever
    # the package provides (here we simulate that by writing the
    # offending value directly under ``esphome.name``).
    yaml_path = tmp_path / "largegarage.yaml"
    yaml_path.write_text("esphome:\n  name: ratgdo.esphome\n")

    device = device_yaml.load_device_from_storage(yaml_path)
    # Address must come from the filename, not the parsed name.
    assert device.address == "largegarage.local"


def test_load_device_from_storage_address_uses_storage_when_set(  # type: ignore[no-untyped-def]
    monkeypatch, tmp_path
) -> None:
    """A real ``StorageJSON.address`` wins over the ``<name>.local`` fallback."""
    monkeypatch.setattr(
        device_yaml,
        "resolve_storage_path",
        lambda config: tmp_path / f"{config}.json",
    )

    class _FakeStorage:
        # Only the fields ``load_device_from_storage`` reads — keeps
        # the test honest about what it's exercising.
        name = "kitchen"
        friendly_name = None
        comment = None
        address = "kitchen.lan"
        web_port = None
        target_platform = ""
        core_platform = None
        firmware_bin_path = None
        esphome_version = ""
        loaded_integrations: ClassVar[list[str]] = []

    monkeypatch.setattr(device_yaml.StorageJSON, "load", staticmethod(lambda _p: _FakeStorage()))

    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n")

    device = device_yaml.load_device_from_storage(yaml_path)
    assert device.address == "kitchen.lan"


def _record_scheduled(coros: list[object]) -> Callable[[object], object]:
    """Build a ``create_background_task`` side-effect that records + closes coroutines.

    Each scheduled coroutine lands in *coros* and is immediately
    closed so it doesn't leak as ``RuntimeWarning: coroutine was
    never awaited``.
    """

    def _impl(coro: object) -> object:
        coros.append(coro)
        if hasattr(coro, "close"):
            coro.close()
        return coro

    return _impl


def test_on_ip_change_persists_non_empty_value() -> None:
    """``_on_ip_change`` schedules a metadata write for non-empty IPs."""
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_ip_change("kitchen", "10.0.0.1", ["10.0.0.1", "fe80::1%en0"])

    assert device.ip == "10.0.0.1"
    assert device.ip_addresses == ["10.0.0.1", "fe80::1%en0"]
    assert len(scheduled) == 1


def test_on_ip_change_skips_persist_for_empty_value() -> None:
    """Empty IP (device went offline) doesn't schedule a write — keeps the cache warm."""
    device = Device(
        name="kitchen",
        friendly_name="Kitchen",
        configuration="kitchen.yaml",
        ip="10.0.0.1",
        ip_addresses=["10.0.0.1"],
    )
    scheduled: list[object] = []
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=_record_scheduled(scheduled)
    )

    controller._on_ip_change("kitchen", "", [])

    assert device.ip == ""
    assert device.ip_addresses == []
    assert scheduled == []


def test_metadata_transaction_serialises_concurrent_writers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """
    Concurrent ``set_device_metadata`` calls from threads can't lose updates.

    Without the module-level lock the load → mutate → save cycle would
    race: two writers would both load the same snapshot, each add their
    own field, and the later save would clobber the earlier. The lock
    serialises the cycle so every write lands.
    """
    writer_count = 32

    def _write(i: int) -> None:
        set_device_metadata(tmp_path, f"device-{i}.yaml", board_id=f"board-{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(writer_count)))

    for i in range(writer_count):
        assert get_board_id(tmp_path, f"device-{i}.yaml") == f"board-{i}"
