"""Tests for the TTL'd DNS cache used by ping pre-resolution + OTA cache args."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers import _dns_cache as dns_cache_mod
from esphome_device_builder.controllers._dns_cache import DNSCache


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
    from esphome_device_builder.models import Device, DeviceState

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
    from esphome_device_builder.controllers import _device_state_monitor as sm
    from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor

    devices = [_device()]
    state_changes: list[tuple[str, object, str]] = []
    ip_changes: list[tuple[str, str]] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda n, s, src: state_changes.append((n, s, src)),
        on_ip_change=lambda n, ip: ip_changes.append((n, ip)),
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
    assert ip_changes == [("kitchen", "10.0.0.1")]
    # DNS cache is now warm — ``get_cached_dns_addresses`` should hit
    # without triggering another resolver call.
    assert monitor.get_cached_dns_addresses("esp.example.com") == ["10.0.0.1"]


async def test_ping_sweep_does_not_apply_ip_for_local_hosts(fake_resolver) -> None:
    """``.local`` devices keep their mDNS-owned IP — DNS doesn't write."""
    from esphome_device_builder.controllers import _device_state_monitor as sm
    from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor

    devices = [_device(address="kitchen.local")]
    ip_changes: list[tuple[str, str]] = []
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda *_: None,
        on_ip_change=lambda n, ip: ip_changes.append((n, ip)),
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

    assert ip_changes == []


async def test_ping_sweep_pings_hostname_when_resolution_fails(fake_resolver) -> None:
    """DNS resolution failure → fall back to pinging the hostname."""
    from esphome_device_builder.controllers import _device_state_monitor as sm
    from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor

    devices = [_device()]
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=lambda *_: None,
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

    assert pinged == ["esp.example.com"]


# ----------------------------------------------------------------------
# IP persistence
# ----------------------------------------------------------------------


def test_set_device_ip_writes_to_metadata(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``set_device_metadata(ip=...)`` round-trips through ``get_device_ip``."""
    from esphome_device_builder.controllers.config import (
        get_device_ip,
        set_device_metadata,
    )

    set_device_metadata(tmp_path, "kitchen.yaml", ip="10.0.0.1")
    assert get_device_ip(tmp_path, "kitchen.yaml") == "10.0.0.1"


def test_set_device_ip_preserves_existing_when_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """
    Empty/None ip leaves the persisted value alone.

    Protects the OTA cache during offline windows — mDNS clears the
    in-memory IP whenever a device drops off, but we still want the
    cache to be warm next time we OTA.
    """
    from esphome_device_builder.controllers.config import (
        get_device_ip,
        set_device_metadata,
    )

    set_device_metadata(tmp_path, "kitchen.yaml", ip="10.0.0.1")
    set_device_metadata(tmp_path, "kitchen.yaml", ip="")
    set_device_metadata(tmp_path, "kitchen.yaml", ip=None)
    assert get_device_ip(tmp_path, "kitchen.yaml") == "10.0.0.1"


def test_set_device_ip_unrelated_field_does_not_clobber_ip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Setting ``board_id`` later doesn't drop the persisted ``ip`` field."""
    from esphome_device_builder.controllers.config import (
        get_device_ip,
        set_device_metadata,
    )

    set_device_metadata(tmp_path, "kitchen.yaml", ip="10.0.0.1")
    set_device_metadata(tmp_path, "kitchen.yaml", board_id="esp32-devkit")
    assert get_device_ip(tmp_path, "kitchen.yaml") == "10.0.0.1"


def test_get_device_ip_returns_empty_for_unknown_device(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from esphome_device_builder.controllers.config import get_device_ip

    assert get_device_ip(tmp_path, "kitchen.yaml") == ""


def test_load_device_from_storage_threads_ip_through(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Scanner-loaded devices carry the persisted IP into the model."""
    from esphome_device_builder.helpers import device_yaml

    # ``ext_storage_path`` walks ``CORE.config_path`` which isn't set
    # in this test; redirect it to ``tmp_path`` and short-circuit
    # ``StorageJSON.load`` so we exercise just the YAML + ip plumbing.
    monkeypatch.setattr(
        device_yaml,
        "ext_storage_path",
        lambda config: tmp_path / f"{config}.json",
    )
    monkeypatch.setattr(device_yaml.StorageJSON, "load", staticmethod(lambda _path: None))

    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n  friendly_name: Kitchen\n")

    device = device_yaml.load_device_from_storage(yaml_path, board_id="esp32-devkit", ip="10.0.0.1")
    assert device.ip == "10.0.0.1"
    assert device.board_id == "esp32-devkit"


def test_on_ip_change_persists_non_empty_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``_on_ip_change`` schedules a metadata write for non-empty IPs."""
    from unittest.mock import MagicMock

    from esphome_device_builder.controllers.devices import DevicesController
    from esphome_device_builder.models import Device

    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    db = MagicMock()
    scheduled: list[object] = []
    db.create_background_task.side_effect = lambda coro: scheduled.append(coro) or coro.close()

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    controller._on_ip_change("kitchen", "10.0.0.1")

    assert device.ip == "10.0.0.1"
    assert len(scheduled) == 1


def test_on_ip_change_skips_persist_for_empty_value() -> None:
    """Empty IP (device went offline) doesn't schedule a write — keeps the cache warm."""
    from unittest.mock import MagicMock

    from esphome_device_builder.controllers.devices import DevicesController
    from esphome_device_builder.models import Device

    device = Device(
        name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml", ip="10.0.0.1"
    )
    db = MagicMock()

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    controller._on_ip_change("kitchen", "")

    assert device.ip == ""
    db.create_background_task.assert_not_called()
