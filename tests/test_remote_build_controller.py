"""
Tests for the phase-2 remote-build controller.

Covers the helper that turns ``AsyncServiceInfo`` into
``RemoteBuildPeer`` plus the WS commands (``list_hosts`` /
``get_settings`` / ``set_settings``). The browser plumbing itself
(``_on_service_state_change``, the resolve task) is exercised by
fabricating ``ServiceStateChange`` events and ``AsyncServiceInfo``
objects directly — no real multicast listener.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zeroconf import ServiceStateChange

from esphome_device_builder.controllers.remote_build import (
    RemoteBuildController,
    _decode_txt_value,
    _peer_from_service_info,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.dashboard_advertise import SERVICE_TYPE
from esphome_device_builder.models import ErrorCode, RemoteBuildPeer, RemoteBuildSettings

# ---------------------------------------------------------------------------
# Helpers used by the tests
# ---------------------------------------------------------------------------


def _fake_service_info(
    *,
    name: str = "desktop",
    server: str = "desktop.local.",
    port: int = 6052,
    addresses: list[str] | None = None,
    server_version: str = "1.2.3",
    esphome_version: str = "2026.5.0",
) -> MagicMock:
    """Build a stand-in for ``AsyncServiceInfo`` carrying the fields we read."""
    info = MagicMock()
    info.name = f"{name}.{SERVICE_TYPE}"
    info.server = server
    info.port = port
    info.parsed_scoped_addresses = MagicMock(return_value=list(addresses or []))
    info.properties = {
        b"server_version": server_version.encode("utf-8"),
        b"esphome_version": esphome_version.encode("utf-8"),
    }
    return info


def _make_controller(*, config_dir: Any = None) -> RemoteBuildController:
    db = MagicMock()
    db.devices = MagicMock()
    db.devices.zeroconf = None
    db._dashboard_advertiser = None
    db.settings = MagicMock()
    db.settings.config_dir = config_dir
    return RemoteBuildController(db)


# ---------------------------------------------------------------------------
# _decode_txt_value
# ---------------------------------------------------------------------------


def test_decode_txt_value_handles_none() -> None:
    assert _decode_txt_value(None) == ""


def test_decode_txt_value_handles_empty_bytes() -> None:
    assert _decode_txt_value(b"") == ""


def test_decode_txt_value_decodes_utf8() -> None:
    assert _decode_txt_value(b"2026.5.0") == "2026.5.0"


def test_decode_txt_value_falls_back_on_invalid_utf8() -> None:
    """A non-utf8 TXT value yields ``""`` instead of raising."""
    assert _decode_txt_value(b"\xff\xff") == ""


# ---------------------------------------------------------------------------
# _peer_from_service_info
# ---------------------------------------------------------------------------


def test_peer_from_service_info_extracts_instance_label() -> None:
    """The peer's ``name`` is the leftmost label of the service-instance name."""
    info = _fake_service_info(name="desktop")
    peer = _peer_from_service_info(f"desktop.{SERVICE_TYPE}", info)
    assert peer.name == "desktop"
    assert peer.hostname == "desktop.local."
    assert peer.port == 6052
    assert peer.server_version == "1.2.3"
    assert peer.esphome_version == "2026.5.0"


def test_peer_from_service_info_carries_all_addresses() -> None:
    info = _fake_service_info(addresses=["192.168.1.10", "fdc8::1"])
    peer = _peer_from_service_info(f"desktop.{SERVICE_TYPE}", info)
    assert peer.addresses == ["192.168.1.10", "fdc8::1"]


def test_peer_from_service_info_preserves_ipv6_scope() -> None:
    """
    IPv6 link-local addresses keep their ``%<interface>`` scope.

    ``parsed_addresses()`` strips the scope suffix; without it
    ``fe80::xxx`` parses but isn't connectable — the OS doesn't
    know which interface to send the packet out on. This test
    pins the choice of ``parsed_scoped_addresses(IPVersion.All)``
    so a future refactor can't quietly switch back.
    """
    info = _fake_service_info(addresses=["fe80::1%en0", "192.168.1.10"])
    peer = _peer_from_service_info(f"desktop.{SERVICE_TYPE}", info)
    assert "fe80::1%en0" in peer.addresses
    assert "192.168.1.10" in peer.addresses


def test_peer_from_service_info_handles_missing_txt_keys() -> None:
    """A peer that didn't broadcast version TXT yields empty version strings."""
    info = _fake_service_info()
    info.properties = {}
    peer = _peer_from_service_info(f"desktop.{SERVICE_TYPE}", info)
    assert peer.server_version == ""
    assert peer.esphome_version == ""


# ---------------------------------------------------------------------------
# Browser callback semantics
# ---------------------------------------------------------------------------


def test_on_service_state_change_filters_own_advertise() -> None:
    """Our own service-instance name never lands in ``_peers``."""
    controller = _make_controller()
    controller._own_instance_name = f"self.{SERVICE_TYPE}"
    zeroconf = MagicMock()
    controller._on_service_state_change(
        zeroconf, SERVICE_TYPE, f"self.{SERVICE_TYPE}", ServiceStateChange.Added
    )
    assert controller._peers == {}


def test_on_service_state_change_removed_drops_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``Removed`` event clears the peer entry immediately."""
    controller = _make_controller()
    controller._peers[f"desktop.{SERVICE_TYPE}"] = RemoteBuildPeer(
        name="desktop", hostname="desktop.local.", port=6052
    )
    controller._on_service_state_change(
        MagicMock(), SERVICE_TYPE, f"desktop.{SERVICE_TYPE}", ServiceStateChange.Removed
    )
    assert controller._peers == {}


def test_on_service_state_change_uses_cache_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-hit resolves the peer synchronously without spawning a task."""
    controller = _make_controller()
    fake_info = _fake_service_info(name="desktop")
    fake_info.load_from_cache = MagicMock(return_value=True)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceInfo",
        MagicMock(return_value=fake_info),
    )
    zeroconf = MagicMock()
    controller._on_service_state_change(
        zeroconf, SERVICE_TYPE, f"desktop.{SERVICE_TYPE}", ServiceStateChange.Added
    )
    assert f"desktop.{SERVICE_TYPE}" in controller._peers
    assert controller._peers[f"desktop.{SERVICE_TYPE}"].name == "desktop"
    # No async resolve task was spawned.
    assert controller._tasks == set()


# ---------------------------------------------------------------------------
# WS commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_hosts_returns_snapshot_of_peers() -> None:
    controller = _make_controller()
    controller._peers[f"desktop.{SERVICE_TYPE}"] = RemoteBuildPeer(
        name="desktop", hostname="desktop.local.", port=6052
    )
    controller._peers[f"laptop.{SERVICE_TYPE}"] = RemoteBuildPeer(
        name="laptop", hostname="laptop.local.", port=6052
    )
    result = await controller.list_hosts()
    assert {peer.name for peer in result} == {"desktop", "laptop"}


@pytest.mark.asyncio
async def test_list_hosts_empty_when_no_peers() -> None:
    controller = _make_controller()
    assert await controller.list_hosts() == []


@pytest.mark.asyncio
async def test_get_settings_defaults_when_unset(tmp_path: Any) -> None:
    """A fresh dashboard with no metadata returns ``enabled=False``."""
    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.get_settings()
    assert settings == RemoteBuildSettings(enabled=False)


@pytest.mark.asyncio
async def test_set_settings_round_trips(tmp_path: Any) -> None:
    """Setting ``enabled=True`` persists and is read back by ``get_settings``."""
    controller = _make_controller(config_dir=tmp_path)
    written = await controller.set_settings(enabled=True)
    assert written == RemoteBuildSettings(enabled=True)
    read = await controller.get_settings()
    assert read == RemoteBuildSettings(enabled=True)


@pytest.mark.asyncio
async def test_set_settings_rejects_non_bool(tmp_path: Any) -> None:
    """
    Non-boolean ``enabled`` raises ``INVALID_ARGS``, doesn't coerce.

    A client sending the string ``"false"`` would coerce to truthy
    under a permissive ``bool()`` cast and silently flip the
    security-sensitive toggle on. Strict ``isinstance`` check
    closes that gap.
    """
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.set_settings(enabled="false")  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_ARGS
    # No write happened — disk still at default.
    settings = await controller.get_settings()
    assert settings.enabled is False


# ---------------------------------------------------------------------------
# Lifecycle no-op paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_skips_when_devices_controller_missing() -> None:
    """``start`` is a no-op when ``DevicesController`` hasn't been set."""
    db = MagicMock()
    db.devices = None
    controller = RemoteBuildController(db)
    await controller.start()
    assert controller._browser is None


@pytest.mark.asyncio
async def test_start_skips_when_zeroconf_unavailable() -> None:
    """``start`` is a no-op when zeroconf failed to bind."""
    controller = _make_controller()
    controller._db.devices.zeroconf = None
    await controller.start()
    assert controller._browser is None


@pytest.mark.asyncio
async def test_start_swallows_browser_construction_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Browser construction failure leaves the controller in a no-peer state.

    Peer discovery is fail-soft — same contract as the advertise.
    A zeroconf-side error during ``AsyncServiceBrowser`` init must
    not crash dashboard startup.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceBrowser",
        MagicMock(side_effect=RuntimeError("zeroconf socket gone")),
    )
    controller = _make_controller()
    controller._db.devices.zeroconf = MagicMock()
    await controller.start()  # must not raise
    assert controller._browser is None


@pytest.mark.asyncio
async def test_start_captures_own_instance_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A registered advertiser's instance name lands in ``_own_instance_name``.

    The browser would otherwise pick up our own broadcast and list
    ourselves as a peer — pin the self-filter wiring through the
    public ``service_instance_name`` accessor.
    """
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock()
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceBrowser",
        MagicMock(return_value=fake_browser),
    )
    controller = _make_controller()
    controller._db.devices.zeroconf = MagicMock()
    advertiser = MagicMock()
    advertiser.service_instance_name = f"self.{SERVICE_TYPE}"
    controller._db._dashboard_advertiser = advertiser

    await controller.start()
    assert controller._own_instance_name == f"self.{SERVICE_TYPE}"
    assert controller._browser is fake_browser
    await controller.stop()


@pytest.mark.asyncio
async def test_start_skips_self_capture_when_advertiser_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered advertiser (HA addon mode etc.) leaves the filter empty."""
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock()
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceBrowser",
        MagicMock(return_value=fake_browser),
    )
    controller = _make_controller()
    controller._db.devices.zeroconf = MagicMock()
    advertiser = MagicMock()
    # ``service_instance_name`` returns ``None`` when the
    # advertiser isn't registered (skipped in HA addon mode or
    # zeroconf failed to bind).
    advertiser.service_instance_name = None
    controller._db._dashboard_advertiser = advertiser

    await controller.start()
    assert controller._own_instance_name is None
    await controller.stop()


@pytest.mark.asyncio
async def test_start_skips_self_capture_when_no_advertiser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entirely-absent advertiser (zeroconf-down branch) is fine."""
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock()
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceBrowser",
        MagicMock(return_value=fake_browser),
    )
    controller = _make_controller()
    controller._db.devices.zeroconf = MagicMock()
    controller._db._dashboard_advertiser = None

    await controller.start()
    assert controller._own_instance_name is None
    await controller.stop()


@pytest.mark.asyncio
async def test_stop_swallows_browser_cancel_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teardown-time browser-cancel failure is logged but not raised."""
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceBrowser",
        MagicMock(return_value=fake_browser),
    )
    controller = _make_controller()
    controller._db.devices.zeroconf = MagicMock()
    await controller.start()
    await controller.stop()  # must not raise
    assert controller._browser is None


@pytest.mark.asyncio
async def test_on_service_state_change_spawns_resolve_task_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-miss queues the async resolve task and tracks it in ``_tasks``."""
    controller = _make_controller()
    fake_info = _fake_service_info(name="desktop")
    fake_info.load_from_cache = MagicMock(return_value=False)
    fake_info.async_request = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceInfo",
        MagicMock(return_value=fake_info),
    )
    zeroconf = MagicMock()
    controller._on_service_state_change(
        zeroconf, SERVICE_TYPE, f"desktop.{SERVICE_TYPE}", ServiceStateChange.Added
    )
    # Drain the resolve task and verify the peer landed.
    pending = list(controller._tasks)
    assert len(pending) == 1
    await asyncio.gather(*pending)
    assert f"desktop.{SERVICE_TYPE}" in controller._peers
    assert controller._tasks == set()


@pytest.mark.asyncio
async def test_resolve_and_apply_swallows_errors() -> None:
    """A resolve-side exception leaves the peer map untouched."""
    controller = _make_controller()
    fake_info = _fake_service_info(name="desktop")
    fake_info.async_request = AsyncMock(side_effect=RuntimeError("network down"))
    await controller._resolve_and_apply(MagicMock(), fake_info, f"desktop.{SERVICE_TYPE}")
    assert controller._peers == {}


@pytest.mark.asyncio
async def test_resolve_and_apply_skips_when_resolution_returns_false() -> None:
    """An ``async_request`` that returns ``False`` (timeout) doesn't add a peer."""
    controller = _make_controller()
    fake_info = _fake_service_info(name="desktop")
    fake_info.async_request = AsyncMock(return_value=False)
    await controller._resolve_and_apply(MagicMock(), fake_info, f"desktop.{SERVICE_TYPE}")
    assert controller._peers == {}


@pytest.mark.asyncio
async def test_stop_drains_resolve_tasks() -> None:
    """In-flight resolve tasks are cancelled and the set is cleared."""
    controller = _make_controller()
    started = asyncio.Event()

    async def _slow() -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(_slow())
    controller._tasks.add(task)
    # Yield so the task body actually begins; otherwise ``cancel``
    # fires against a never-started task and the test isn't
    # exercising the drain.
    await started.wait()
    await controller.stop()
    assert task.done()
    assert controller._tasks == set()
