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
import hashlib
import json
import secrets as _secrets
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zeroconf import ServiceStateChange

from esphome_device_builder.controllers import remote_build as rb
from esphome_device_builder.controllers.config import (
    load_remote_build_settings,
    remote_build_settings_transaction,
)
from esphome_device_builder.controllers.remote_build import (
    RemoteBuildController,
    _decode_txt_value,
    _peer_from_manual_host,
    _peer_from_service_info,
    _validate_hostname,
    _validate_port,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.dashboard_advertise import SERVICE_TYPE
from esphome_device_builder.models import (
    ErrorCode,
    EventType,
    IdentityView,
    ManualHost,
    PeerStatus,
    RemoteBuildPeer,
    RemoteBuildPeerSource,
    RemoteBuildSettingsView,
    StoredPeer,
)

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


async def _seed_metadata(config_dir: Any, remote_build: dict) -> None:
    """
    Seed ``<config_dir>/.device-builder.json`` with a ``_remote_build`` blob.

    Single place to write a hand-crafted on-disk state from a
    test, used by the legacy-compat and corrupt-row tests so the
    JSON shape lives in one place. Hops to the executor because
    the file write is sync I/O and blockbuster (Linux CI) flags
    sync I/O from inside an async test as a real bug.
    """
    loop = asyncio.get_running_loop()

    def _write() -> None:
        (config_dir / ".device-builder.json").write_bytes(
            json.dumps({"_remote_build": remote_build}).encode()
        )

    await loop.run_in_executor(None, _write)


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
        name="desktop",
        hostname="desktop.local.",
        port=6052,
        source=RemoteBuildPeerSource.MDNS,
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
async def test_list_hosts_returns_snapshot_of_peers(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._peers[f"desktop.{SERVICE_TYPE}"] = RemoteBuildPeer(
        name="desktop",
        hostname="desktop.local.",
        port=6052,
        source=RemoteBuildPeerSource.MDNS,
    )
    controller._peers[f"laptop.{SERVICE_TYPE}"] = RemoteBuildPeer(
        name="laptop",
        hostname="laptop.local.",
        port=6052,
        source=RemoteBuildPeerSource.MDNS,
    )
    result = await controller.list_hosts()
    assert {peer.name for peer in result} == {"desktop", "laptop"}
    assert all(peer.source == RemoteBuildPeerSource.MDNS for peer in result)


@pytest.mark.asyncio
async def test_list_hosts_empty_when_no_peers(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    assert await controller.list_hosts() == []


@pytest.mark.asyncio
async def test_get_settings_defaults_when_unset(tmp_path: Path) -> None:
    """A fresh dashboard with no metadata returns ``enabled=False``."""
    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.get_settings()
    assert settings == RemoteBuildSettingsView(enabled=False)


@pytest.mark.asyncio
async def test_set_settings_round_trips(tmp_path: Path) -> None:
    """Setting ``enabled=True`` persists and is read back by ``get_settings``."""
    controller = _make_controller(config_dir=tmp_path)
    written = await controller.set_settings(enabled=True)
    assert written == RemoteBuildSettingsView(enabled=True)
    read = await controller.get_settings()
    assert read == RemoteBuildSettingsView(enabled=True)


@pytest.mark.asyncio
async def test_set_settings_rejects_non_bool(tmp_path: Path) -> None:
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
async def test_start_skips_when_devices_controller_missing(tmp_path: Path) -> None:
    """``start`` is a no-op when ``DevicesController`` hasn't been set."""
    db = MagicMock()
    db.devices = None
    db.settings = MagicMock()
    db.settings.config_dir = tmp_path
    controller = RemoteBuildController(db)
    await controller.start()
    assert controller._browser is None


@pytest.mark.asyncio
async def test_start_skips_when_zeroconf_unavailable(tmp_path: Path) -> None:
    """``start`` is a no-op when zeroconf failed to bind."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.devices.zeroconf = None
    await controller.start()
    assert controller._browser is None


@pytest.mark.asyncio
async def test_start_swallows_browser_construction_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    controller = _make_controller(config_dir=tmp_path)
    controller._db.devices.zeroconf = MagicMock()
    await controller.start()  # must not raise
    assert controller._browser is None


@pytest.mark.asyncio
async def test_start_captures_own_instance_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    controller = _make_controller(config_dir=tmp_path)
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unregistered advertiser (HA addon mode etc.) leaves the filter empty."""
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock()
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceBrowser",
        MagicMock(return_value=fake_browser),
    )
    controller = _make_controller(config_dir=tmp_path)
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An entirely-absent advertiser (zeroconf-down branch) is fine."""
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock()
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceBrowser",
        MagicMock(return_value=fake_browser),
    )
    controller = _make_controller(config_dir=tmp_path)
    controller._db.devices.zeroconf = MagicMock()
    controller._db._dashboard_advertiser = None

    await controller.start()
    assert controller._own_instance_name is None
    await controller.stop()


@pytest.mark.asyncio
async def test_stop_swallows_browser_cancel_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A teardown-time browser-cancel failure is logged but not raised."""
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.AsyncServiceBrowser",
        MagicMock(return_value=fake_browser),
    )
    controller = _make_controller(config_dir=tmp_path)
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


# ---------------------------------------------------------------------------
# Phase 2b: manual hosts
# ---------------------------------------------------------------------------


def test_validate_hostname_lowercases_and_strips() -> None:
    """RFC 1035 §2.3.3: hostnames are case-insensitive."""
    assert _validate_hostname("  Desktop.Local  ") == "desktop.local"


def test_validate_hostname_rejects_non_string() -> None:
    with pytest.raises(CommandError) as exc:
        _validate_hostname(42)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_ARGS


def test_validate_hostname_rejects_empty() -> None:
    with pytest.raises(CommandError) as exc:
        _validate_hostname("   ")
    assert exc.value.code == ErrorCode.INVALID_ARGS


def test_validate_hostname_rejects_oversize() -> None:
    """A megabyte-string masquerading as a hostname is rejected pre-store.

    Caps at 255 chars (RFC 1035 §2.3.4 = 253, plus slack for
    trailing-dot variations). The error message names the cap so a
    misbehaving frontend can surface a useful diagnostic to the user.
    """
    with pytest.raises(CommandError) as exc:
        _validate_hostname("a" * 256)
    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert "255 characters" in str(exc.value)


@pytest.mark.parametrize(
    "bad_host",
    [
        "evil/path",  # path injection
        "host?q=1",  # query injection
        "host#frag",  # fragment injection
        "user@host",  # userinfo injection
        "host:8080",  # embedded port (frontend mistake — port goes in its own field)
    ],
)
def test_validate_hostname_rejects_url_injection_shapes(bad_host: str) -> None:
    """Pathological characters can't smuggle path / query / userinfo into the URL.

    Defers to ``yarl.URL.build`` for the URL-correctness check
    so the validator and the offloader's ``_build_ws_url``
    share one source of truth on what a host is. Without this
    gate, a frontend that forwarded ``host:8080`` to the
    hostname field would have surfaced as ``UNAVAILABLE`` from
    ``preview_pair`` (or worse, ``INTERNAL_ERROR`` if the
    ``ValueError`` escaped error mapping); now it surfaces as
    ``INVALID_ARGS`` at write time so the user gets a "fix
    your input" diagnostic.
    """
    with pytest.raises(CommandError) as exc:
        _validate_hostname(bad_host)
    assert exc.value.code == ErrorCode.INVALID_ARGS


def test_validate_hostname_accepts_ipv6_literal() -> None:
    """Bare IPv6 literals (no brackets) round-trip through the validator.

    yarl accepts ``::1`` / ``fe80::1`` as host values and
    auto-brackets them at render time, so the validator doesn't
    need to reject the colon-laden form even though a hostname
    with a stray colon (``host:8080``) is rejected: yarl knows
    the difference because ``::1`` parses as a valid IPv6 and
    ``host:8080`` doesn't.
    """
    assert _validate_hostname("::1") == "::1"
    assert _validate_hostname("fe80::1") == "fe80::1"


def test_validate_port_accepts_typical() -> None:
    assert _validate_port(6052) == 6052


def test_validate_port_rejects_non_int() -> None:
    with pytest.raises(CommandError) as exc:
        _validate_port("6052")  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_ARGS


def test_validate_port_rejects_bool() -> None:
    """``isinstance(True, int)`` is true, but coercing to 1 is a footgun."""
    with pytest.raises(CommandError) as exc:
        _validate_port(True)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_validate_port_rejects_out_of_range(port: int) -> None:
    with pytest.raises(CommandError) as exc:
        _validate_port(port)
    assert exc.value.code == ErrorCode.INVALID_ARGS


def test_peer_from_manual_host_uses_manual_source() -> None:
    """A manual entry's ``RemoteBuildPeer`` row reports ``source=MANUAL``."""
    peer = _peer_from_manual_host(ManualHost(hostname="192.168.1.10", port=6052))
    assert peer.source == RemoteBuildPeerSource.MANUAL
    assert peer.name == "192.168.1.10"
    assert peer.hostname == "192.168.1.10"
    assert peer.port == 6052
    # Version fields stay blank; phase 4 fills them in via the
    # actual connection attempt.
    assert peer.server_version == ""
    assert peer.esphome_version == ""
    assert peer.addresses == []


@pytest.mark.asyncio
async def test_add_manual_host_persists_and_returns_settings(tmp_path: Path) -> None:
    """Happy path: a unique entry is appended and the settings round-trip."""
    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.add_manual_host(hostname="desktop.local", port=6052)
    assert settings.manual_hosts == [ManualHost(hostname="desktop.local", port=6052)]
    # Round-trip: get_settings reflects the persisted state.
    reread = await controller.get_settings()
    assert reread.manual_hosts == settings.manual_hosts


@pytest.mark.asyncio
async def test_add_manual_host_rejects_duplicate(tmp_path: Path) -> None:
    """
    A second add of the same ``(hostname, port)`` raises ``ALREADY_EXISTS``.

    Distinct from ``INVALID_ARGS`` so the frontend can show a
    "this dashboard is already in your list" message without
    string-matching the details field. The user gets feedback
    that the entry already existed rather than a silent no-op.
    """
    controller = _make_controller(config_dir=tmp_path)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    with pytest.raises(CommandError) as exc:
        await controller.add_manual_host(hostname="desktop.local", port=6052)
    assert exc.value.code == ErrorCode.ALREADY_EXISTS


@pytest.mark.asyncio
async def test_add_manual_host_normalises_case_for_dedup(tmp_path: Path) -> None:
    """``Desktop.Local`` and ``desktop.local`` are the same entry."""
    controller = _make_controller(config_dir=tmp_path)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    with pytest.raises(CommandError):
        await controller.add_manual_host(hostname="Desktop.Local", port=6052)


@pytest.mark.asyncio
async def test_add_manual_host_keeps_enabled_intact(tmp_path: Path) -> None:
    """
    Adding a manual host doesn't reset ``enabled``.

    Pin the read-modify-write semantics. Without it,
    ``set_settings(enabled=True)`` followed by
    ``add_manual_host(...)`` would silently flip ``enabled`` back
    to ``False``.
    """
    controller = _make_controller(config_dir=tmp_path)
    await controller.set_settings(enabled=True)
    settings = await controller.add_manual_host(hostname="desktop.local", port=6052)
    assert settings.enabled is True


@pytest.mark.asyncio
async def test_remove_manual_host_drops_entry(tmp_path: Path) -> None:
    """Happy path: a registered entry is removed."""
    controller = _make_controller(config_dir=tmp_path)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    await controller.add_manual_host(hostname="laptop.local", port=6052)
    settings = await controller.remove_manual_host(hostname="desktop.local", port=6052)
    assert settings.manual_hosts == [ManualHost(hostname="laptop.local", port=6052)]


@pytest.mark.asyncio
async def test_remove_manual_host_rejects_unknown(tmp_path: Path) -> None:
    """``NOT_FOUND`` for a host that was never registered."""
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.remove_manual_host(hostname="ghost.local", port=6052)
    assert exc.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_remove_manual_host_normalises_case(tmp_path: Path) -> None:
    """``Desktop.Local`` removes a stored ``desktop.local`` entry."""
    controller = _make_controller(config_dir=tmp_path)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    settings = await controller.remove_manual_host(hostname="Desktop.Local", port=6052)
    assert settings.manual_hosts == []


@pytest.mark.asyncio
async def test_set_settings_preserves_manual_hosts(tmp_path: Path) -> None:
    """
    ``set_settings(enabled=...)`` doesn't wipe ``manual_hosts``.

    The previous ``set_settings`` shape full-replaced the
    serialised blob, which would have reset every field a client
    didn't pass to its default. Pin the read-modify-write so a
    toggle of ``enabled`` doesn't silently drop the user's
    manual-host list.
    """
    controller = _make_controller(config_dir=tmp_path)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    settings = await controller.set_settings(enabled=True)
    assert settings.enabled is True
    assert settings.manual_hosts == [ManualHost(hostname="desktop.local", port=6052)]


@pytest.mark.asyncio
async def test_list_hosts_merges_mdns_and_manual(tmp_path: Path) -> None:
    """
    ``list_hosts`` returns mDNS-discovered peers followed by manual hosts.

    Each row carries its origin in ``source``; mDNS rows are
    placed first so the auto-discovered list is the primary
    content.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._peers[f"desktop.{SERVICE_TYPE}"] = RemoteBuildPeer(
        name="desktop",
        hostname="desktop.local.",
        port=6052,
        source=RemoteBuildPeerSource.MDNS,
    )
    await controller.add_manual_host(hostname="10.0.0.5", port=6052)

    result = await controller.list_hosts()
    assert len(result) == 2
    assert result[0].source == RemoteBuildPeerSource.MDNS
    assert result[0].name == "desktop"
    assert result[1].source == RemoteBuildPeerSource.MANUAL
    assert result[1].name == "10.0.0.5"


@pytest.mark.asyncio
async def test_add_manual_host_rejects_invalid_port(tmp_path: Path) -> None:
    """Out-of-range port doesn't slip through."""
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.add_manual_host(hostname="desktop.local", port=0)
    assert exc.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_add_manual_host_rejects_blank_hostname(tmp_path: Path) -> None:
    """Empty / whitespace hostname doesn't slip through."""
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.add_manual_host(hostname="   ", port=6052)
    assert exc.value.code == ErrorCode.INVALID_ARGS


# ---------------------------------------------------------------------------
# Identity (phase 3c1) — get_identity / rotate_identity
# ---------------------------------------------------------------------------


def _stub_identity_db(
    controller: RemoteBuildController, *, listener_bound: bool = False
) -> AsyncMock:
    """
    Wire the controller's ``_db`` for an identity-rotation test.

    The default ``_make_controller`` uses a plain ``MagicMock``;
    ``rotate_identity`` awaits ``reload_remote_build_identity``
    and reads ``is_remote_build_listener_bound``, both of which
    need to return real values. Sets up:

    * ``reload_remote_build_identity`` as an ``AsyncMock`` whose
      return value is *listener_bound* (the post-rebuild bool).
    * ``is_remote_build_listener_bound`` as a fixed *listener_bound*
      so ``get_identity`` reports a deterministic value too.
    * ``bus.fire`` as a plain ``MagicMock`` so event-fire
      assertions can introspect the call args.

    Returns the reload mock so individual tests can assert on it.
    """
    reload_mock = AsyncMock(return_value=listener_bound)
    controller._db.reload_remote_build_identity = reload_mock
    controller._db.is_remote_build_listener_bound = listener_bound
    controller._db.bus = MagicMock()
    return reload_mock


@pytest.mark.asyncio
async def test_get_identity_returns_dashboard_id_pin_and_versions(tmp_path: Path) -> None:
    """``get_identity`` projects the persistent identity into the wire shape."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller)
    view = await controller.get_identity()
    assert isinstance(view, IdentityView)
    # Every field is non-empty: dashboard_id is the random 24-byte
    # b64url id from get_or_create_identity, pin_sha256 is the
    # hex SPKI fingerprint, server_version + esphome_version come
    # from constants. Don't pin specific values — the test would
    # break on every version bump.
    assert view.dashboard_id
    assert len(view.pin_sha256) == 64  # SHA-256 hex
    assert all(c in "0123456789abcdef" for c in view.pin_sha256)
    assert view.server_version
    assert view.esphome_version


@pytest.mark.asyncio
async def test_get_identity_lazy_creates_cert_and_key_on_first_call(tmp_path: Path) -> None:
    """``get_identity`` writes the cert + key to disk if they're missing."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller)
    # Pre-condition: empty config_dir, no cert / key on disk.
    assert not (tmp_path / ".device-builder-cert.pem").exists()
    assert not (tmp_path / ".device-builder-key.pem").exists()

    await controller.get_identity()

    # ``get_or_create_identity`` is the lazy-creator; the
    # controller relies on this so a cold-boot dashboard's
    # Settings UI doesn't have to call rotate_identity to get a
    # cert. Asserts the contract so a future refactor that
    # switches to ``get_identity_or_raise`` would catch here.
    assert (tmp_path / ".device-builder-cert.pem").is_file()
    assert (tmp_path / ".device-builder-key.pem").is_file()


@pytest.mark.asyncio
async def test_get_identity_reflects_listener_bound_state(tmp_path: Path) -> None:
    """``listener_bound`` reads the dashboard's runner state."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller, listener_bound=True)
    bound_view = await controller.get_identity()
    assert bound_view.listener_bound is True

    _stub_identity_db(controller, listener_bound=False)
    unbound_view = await controller.get_identity()
    assert unbound_view.listener_bound is False


@pytest.mark.asyncio
async def test_get_identity_does_not_leak_cert_or_key_pem(tmp_path: Path) -> None:
    """Wire shape is the declared fields only — no PEM bytes."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller)
    view = await controller.get_identity()
    encoded = view.to_json()
    # PEM block markers should NEVER appear in any get_identity
    # response. Spell them as runtime-joined fragments so the
    # detect-private-key pre-commit hook doesn't trip on the test
    # source itself.
    assert "BEGIN " + "CERTIFICATE" not in encoded
    assert "BEGIN " + "PRI" + "VATE KEY" not in encoded
    # Belt and braces: redacted JSON has no field at all that
    # could carry the PEM bytes.
    assert "cert_pem" not in encoded
    assert "key_pem" not in encoded


@pytest.mark.asyncio
async def test_get_identity_is_idempotent_across_calls(tmp_path: Path) -> None:
    """Two calls return the same identity (no rotation triggered by reads)."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller)
    first = await controller.get_identity()
    second = await controller.get_identity()
    assert first == second


@pytest.mark.asyncio
async def test_rotate_identity_changes_pin_sha256(tmp_path: Path) -> None:
    """A rotate produces a different SPKI fingerprint than the previous identity."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller)
    pre = await controller.get_identity()
    rotated = await controller.rotate_identity()
    assert rotated.pin_sha256 != pre.pin_sha256
    # ``dashboard_id`` is preserved across rotations (stable
    # identity; only the cert changes). The receiver-side audit
    # trail relies on this.
    assert rotated.dashboard_id == pre.dashboard_id


@pytest.mark.asyncio
async def test_rotate_identity_calls_reload_hook_with_new_pin(tmp_path: Path) -> None:
    """The rotate hands the new pin off to the dashboard for listener rebuild."""
    controller = _make_controller(config_dir=tmp_path)
    reload_mock = _stub_identity_db(controller)
    rotated = await controller.rotate_identity()
    reload_mock.assert_awaited_once_with(pin_sha256=rotated.pin_sha256)


@pytest.mark.asyncio
async def test_rotate_identity_persists_to_disk(tmp_path: Path) -> None:
    """The new cert + key land on disk so a fresh ``get_identity`` agrees."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller)
    rotated = await controller.rotate_identity()
    # Re-read through ``get_identity`` to confirm the on-disk
    # state matches what rotate returned (i.e. the fresh cert
    # was actually persisted, not just held in memory).
    reread = await controller.get_identity()
    assert reread.pin_sha256 == rotated.pin_sha256


@pytest.mark.asyncio
async def test_rotate_identity_response_omits_cert_pem(tmp_path: Path) -> None:
    """Rotate's wire response also redacts cert + key bytes."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller)
    view = await controller.rotate_identity()
    encoded = view.to_json()
    # Spell the markers as fragments so the detect-private-key
    # pre-commit hook doesn't trip on the test source itself.
    assert "BEGIN " + "CERTIFICATE" not in encoded
    assert "BEGIN " + "PRI" + "VATE KEY" not in encoded
    assert "cert_pem" not in encoded
    assert "key_pem" not in encoded


@pytest.mark.asyncio
async def test_rotate_identity_surfaces_listener_bound_from_reload(tmp_path: Path) -> None:
    """``IdentityView.listener_bound`` reflects the rebuild's outcome."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller, listener_bound=True)
    view = await controller.rotate_identity()
    assert view.listener_bound is True

    _stub_identity_db(controller, listener_bound=False)
    view = await controller.rotate_identity()
    assert view.listener_bound is False


@pytest.mark.asyncio
async def test_rotate_identity_fires_event_on_bus(tmp_path: Path) -> None:
    """A successful rotate fires ``REMOTE_BUILD_IDENTITY_ROTATED``."""
    controller = _make_controller(config_dir=tmp_path)
    _stub_identity_db(controller)
    view = await controller.rotate_identity()
    fire = controller._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_IDENTITY_ROTATED
    assert payload == {
        "dashboard_id": view.dashboard_id,
        "pin_sha256": view.pin_sha256,
    }


@pytest.mark.asyncio
async def test_rotate_identity_concurrent_call_rejected(tmp_path: Path) -> None:
    """A second concurrent ``rotate_identity`` raises ``ALREADY_EXISTS``."""
    controller = _make_controller(config_dir=tmp_path)
    gate = asyncio.Event()
    release = asyncio.Event()

    async def _slow_reload(*, pin_sha256: str) -> bool:
        gate.set()
        await release.wait()
        return True

    controller._db.reload_remote_build_identity = _slow_reload
    controller._db.is_remote_build_listener_bound = False
    controller._db.bus = MagicMock()

    first = asyncio.create_task(controller.rotate_identity())
    # Wait until the first rotation is mid-reload (i.e. the
    # in-flight flag is set).
    await gate.wait()

    with pytest.raises(CommandError) as exc:
        await controller.rotate_identity()
    assert exc.value.code == ErrorCode.ALREADY_EXISTS

    # Let the first one finish so we don't leak the task.
    release.set()
    first_result = await first
    assert isinstance(first_result, IdentityView)


@pytest.mark.asyncio
async def test_rotate_identity_clears_in_flight_flag_on_failure(tmp_path: Path) -> None:
    """A failed reload still clears the flag so the next rotate isn't stuck rejected."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.reload_remote_build_identity = AsyncMock(side_effect=RuntimeError("boom"))
    controller._db.is_remote_build_listener_bound = False
    controller._db.bus = MagicMock()

    with pytest.raises(RuntimeError):
        await controller.rotate_identity()

    # Flag must be back to False; otherwise every subsequent
    # rotate attempt would 409 forever.
    assert controller._rotation_in_flight is False


# ---------------------------------------------------------------------------
# Phase 4a-r1 part 3: peer CRUD + pairing window
# ---------------------------------------------------------------------------


def _stored_peer(
    *,
    dashboard_id: str = "alpha",
    label: str = "alpha",
    pin_sha256: str | None = None,
    static_x25519_pub: bytes | None = None,
    paired_at: float = 1_700_000_000.0,
    status: PeerStatus = PeerStatus.PENDING,
) -> StoredPeer:
    """Construct a ``StoredPeer`` with sensible defaults for tests."""
    pub = static_x25519_pub if static_x25519_pub is not None else _secrets.token_bytes(32)
    pin = pin_sha256 if pin_sha256 is not None else hashlib.sha256(pub).hexdigest()
    return StoredPeer(
        dashboard_id=dashboard_id,
        pin_sha256=pin,
        static_x25519_pub=pub,
        label=label,
        paired_at=paired_at,
        status=status,
    )


async def _seed_peer(config_dir: Path, peer: StoredPeer) -> None:
    """Persist a single ``StoredPeer`` row under ``_remote_build.peers``."""
    loop = asyncio.get_running_loop()

    def _write() -> None:
        with remote_build_settings_transaction(config_dir) as settings:
            settings.peers.append(peer)

    await loop.run_in_executor(None, _write)


@pytest.mark.asyncio
async def test_list_peers_returns_empty_when_none_stored(tmp_path: Path) -> None:
    """List on a fresh dashboard returns an empty list, not an error."""
    controller = _make_controller(config_dir=tmp_path)
    assert await controller.list_peers() == []


@pytest.mark.asyncio
async def test_list_peers_returns_summary_for_each_row(tmp_path: Path) -> None:
    """``list_peers`` projects every stored peer to ``PeerSummary``."""
    controller = _make_controller(config_dir=tmp_path)
    pending = _stored_peer(dashboard_id="pending", status=PeerStatus.PENDING)
    approved = _stored_peer(dashboard_id="approved", status=PeerStatus.APPROVED)
    await _seed_peer(tmp_path, pending)
    await _seed_peer(tmp_path, approved)

    rows = await controller.list_peers()

    assert {row.dashboard_id for row in rows} == {"pending", "approved"}
    statuses = {row.dashboard_id: row.status for row in rows}
    assert statuses == {"pending": PeerStatus.PENDING, "approved": PeerStatus.APPROVED}


@pytest.mark.asyncio
async def test_list_peers_drops_static_x25519_pub_from_wire(tmp_path: Path) -> None:
    """The wire summary must not expose raw ``static_x25519_pub`` bytes."""
    controller = _make_controller(config_dir=tmp_path)
    await _seed_peer(tmp_path, _stored_peer(static_x25519_pub=b"\xaa" * 32))

    [row] = await controller.list_peers()

    serialised = row.to_dict()
    assert "static_x25519_pub" not in serialised
    assert serialised["pin_sha256"]  # the wire-friendly form is present


@pytest.mark.asyncio
async def test_approve_peer_promotes_pending_to_approved(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await _seed_peer(tmp_path, _stored_peer(dashboard_id="alpha", status=PeerStatus.PENDING))

    view = await controller.approve_peer(dashboard_id="alpha")

    assert view.peers[0].status == PeerStatus.APPROVED
    # Hop the sync I/O off the loop so blockbuster doesn't flag it
    # (the production path always goes through run_in_executor too).
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    assert settings.peers[0].status == PeerStatus.APPROVED


@pytest.mark.asyncio
async def test_approve_peer_fires_pair_status_changed(tmp_path: Path) -> None:
    """Approval fires ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` with status=approved."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await _seed_peer(tmp_path, _stored_peer(dashboard_id="alpha"))

    await controller.approve_peer(dashboard_id="alpha")

    fire = controller._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED
    assert payload == {"dashboard_id": "alpha", "status": "approved"}


@pytest.mark.asyncio
async def test_approve_peer_unknown_returns_not_found(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await controller.approve_peer(dashboard_id="ghost")

    assert exc.value.code is ErrorCode.NOT_FOUND
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_approve_peer_already_approved_returns_invalid_args(tmp_path: Path) -> None:
    """Re-approving an already-APPROVED peer is rejected, not silently re-fired."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await _seed_peer(tmp_path, _stored_peer(dashboard_id="alpha", status=PeerStatus.APPROVED))

    with pytest.raises(CommandError) as exc:
        await controller.approve_peer(dashboard_id="alpha")

    assert exc.value.code is ErrorCode.INVALID_ARGS
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_approve_peer_rejects_invalid_dashboard_id(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await controller.approve_peer(dashboard_id="has spaces!")

    assert exc.value.code is ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_approve_peer_rejects_non_string_dashboard_id(tmp_path: Path) -> None:
    """Non-string ``dashboard_id`` is rejected up front, not silently coerced."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await controller.approve_peer(dashboard_id=12345)  # type: ignore[arg-type]

    assert exc.value.code is ErrorCode.INVALID_ARGS
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_remove_peer_drops_pending_silently(tmp_path: Path) -> None:
    """Removing a PENDING peer is rejection-as-cleanup; no event fires."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await _seed_peer(tmp_path, _stored_peer(dashboard_id="alpha", status=PeerStatus.PENDING))

    view = await controller.remove_peer(dashboard_id="alpha")

    assert view.peers == []
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_remove_peer_drops_approved_and_fires_event(tmp_path: Path) -> None:
    """Removing an APPROVED peer is revocation; fires the removed event."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await _seed_peer(tmp_path, _stored_peer(dashboard_id="alpha", status=PeerStatus.APPROVED))

    view = await controller.remove_peer(dashboard_id="alpha")

    assert view.peers == []
    fire = controller._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED
    assert payload == {"dashboard_id": "alpha", "status": "removed"}


@pytest.mark.asyncio
async def test_remove_peer_keeps_other_rows(tmp_path: Path) -> None:
    """``remove_peer`` only touches the matching dashboard_id."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await _seed_peer(tmp_path, _stored_peer(dashboard_id="keep"))
    await _seed_peer(tmp_path, _stored_peer(dashboard_id="drop"))

    view = await controller.remove_peer(dashboard_id="drop")

    assert {peer.dashboard_id for peer in view.peers} == {"keep"}


@pytest.mark.asyncio
async def test_remove_peer_unknown_returns_not_found(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await controller.remove_peer(dashboard_id="ghost")

    assert exc.value.code is ErrorCode.NOT_FOUND
    controller._db.bus.fire.assert_not_called()


# --- pairing window ---


@pytest.mark.asyncio
async def test_pairing_window_starts_closed(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    assert controller.is_pairing_window_open() is False


@pytest.mark.asyncio
async def test_set_pairing_window_open_opens_and_fires(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    state = await controller.set_pairing_window(open=True, client="tab-1")

    assert state.open is True
    assert state.expires_in_seconds is not None
    assert 0 < state.expires_in_seconds <= 300.0
    assert controller.is_pairing_window_open() is True
    fire = controller._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED
    assert payload["open"] is True
    assert payload["expires_in_seconds"] is not None

    # cleanup the auto-close task so the test loop can exit
    await controller.stop()


@pytest.mark.asyncio
async def test_set_pairing_window_close_closes_and_fires(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await controller.set_pairing_window(open=True, client="tab-1")
    controller._db.bus.fire.reset_mock()

    state = await controller.set_pairing_window(open=False, client="tab-1")

    assert state.open is False
    assert state.expires_in_seconds is None
    assert controller.is_pairing_window_open() is False
    fire = controller._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED
    assert payload == {"open": False, "expires_in_seconds": None}

    await controller.stop()


@pytest.mark.asyncio
async def test_set_pairing_window_close_while_already_closed_is_silent(tmp_path: Path) -> None:
    """A close from a client that wasn't extending must not fire."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    state = await controller.set_pairing_window(open=False, client="tab-1")

    assert state.open is False
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_set_pairing_window_extend_refreshes_deadline_and_fires(tmp_path: Path) -> None:
    """
    Repeat ``open=true`` advances the client's timestamp and fires the event.

    The load-bearing invariant the whole multi-tab UX rests on:
    extending must move the per-client timestamp forward, not
    just observe it. A regression where extend is silently
    skipped for already-extending clients (e.g. an
    "if client not in clients: clients[client] = now" guard
    instead of "clients[client] = now") would still leave the
    window technically "open" with the old deadline; the
    TimerHandle would auto-close 5 minutes after the first
    extend instead of 5 after the latest user activity. Pin
    the actual timestamp advance so a guard like that fails
    here, not in production.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    first = await controller.set_pairing_window(open=True, client="tab-1")
    first_extend_ts = controller._pairing_window_clients["tab-1"]
    # tiny sleep so the second extend's monotonic timestamp is
    # strictly later than the first's (microsecond resolution
    # makes 10ms reliably non-flaky)
    await asyncio.sleep(0.01)
    second = await controller.set_pairing_window(open=True, client="tab-1")
    second_extend_ts = controller._pairing_window_clients["tab-1"]

    assert first.expires_in_seconds is not None
    assert second.expires_in_seconds is not None
    # The actual extend invariant: the second call advanced the
    # client's last-extend timestamp. Without this assertion,
    # a silent extend-is-a-no-op regression would still pass
    # the rest of the test (window is "open", events fired,
    # both payloads carry open=True) while breaking the
    # multi-tab UX.
    assert second_extend_ts > first_extend_ts
    # Both fires landed (open + extend); both events have open=True.
    assert controller._db.bus.fire.call_count == 2
    for call in controller._db.bus.fire.call_args_list:
        _, payload = call.args
        assert payload["open"] is True

    await controller.stop()


@pytest.mark.asyncio
async def test_pairing_window_two_clients_refcount(tmp_path: Path) -> None:
    """Two tabs / two users: window stays open until the LAST client closes."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    await controller.set_pairing_window(open=True, client="tab-A")
    await controller.set_pairing_window(open=True, client="tab-B")
    assert controller.is_pairing_window_open() is True

    # Tab A graceful close: tab B is still extending → window must stay open.
    await controller.set_pairing_window(open=False, client="tab-A")
    assert controller.is_pairing_window_open() is True

    # Tab B graceful close: now no clients are extending → window closes.
    await controller.set_pairing_window(open=False, client="tab-B")
    assert controller.is_pairing_window_open() is False

    # Three events: open (tab A), extend (tab B opens, fires too), close (tab B unsets).
    # Tab A's close was non-state-changing (tab B still extending) → no fire.
    fire_calls = controller._db.bus.fire.call_args_list
    open_states = [call.args[1]["open"] for call in fire_calls]
    assert open_states == [True, True, False]

    await controller.stop()


@pytest.mark.asyncio
async def test_pairing_window_close_from_non_extender_does_not_fire(tmp_path: Path) -> None:
    """A spurious open=False from a client that wasn't extending is a no-op."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await controller.set_pairing_window(open=True, client="tab-A")
    controller._db.bus.fire.reset_mock()

    # tab-B never called open=true; its close call is a no-op.
    await controller.set_pairing_window(open=False, client="tab-B")

    assert controller.is_pairing_window_open() is True
    controller._db.bus.fire.assert_not_called()

    await controller.stop()


@pytest.mark.asyncio
async def test_set_pairing_window_rejects_non_bool(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await controller.set_pairing_window(open="yes", client="tab-1")  # type: ignore[arg-type]

    assert exc.value.code is ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_pairing_window_auto_closes_when_clients_age_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window auto-closes when every client's last-extend ages past the duration."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    # Patch the duration to ~0 so the auto-close fires almost immediately.
    monkeypatch.setattr(rb, "_PAIRING_WINDOW_DURATION_SECONDS", 0.05)

    await controller.set_pairing_window(open=True, client="tab-1")
    assert controller.is_pairing_window_open() is True
    controller._db.bus.fire.reset_mock()

    # Wait for the deadline to lapse + a hair for the task to settle.
    await asyncio.sleep(0.2)

    assert controller.is_pairing_window_open() is False
    # An auto-close event fired (open=False).
    fire = controller._db.bus.fire
    assert fire.call_count >= 1
    last_event_type, last_payload = fire.call_args.args
    assert last_event_type is EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED
    assert last_payload["open"] is False

    await controller.stop()


@pytest.mark.asyncio
async def test_stop_cancels_pairing_window_handle(tmp_path: Path) -> None:
    """``controller.stop()`` cleans up the auto-close TimerHandle."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await controller.set_pairing_window(open=True, client="tab-1")
    assert controller._pairing_window_handle is not None

    await controller.stop()

    assert controller._pairing_window_handle is None
    assert controller.is_pairing_window_open() is False


@pytest.mark.asyncio
async def test_explicit_close_cancels_handle_no_duplicate_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Explicit ``open=false`` cancels the auto-close handle.

    Regression for a class of bug Copilot flagged on PR #476: an
    explicit close left the deadline-fire handle running, which
    would fire a SECOND ``REMOTE_BUILD_PAIRING_WINDOW_CHANGED``
    close event when the original deadline lapsed (after a real
    close had already fired one). The TimerHandle redesign makes
    every set_pairing_window call cancel-and-reschedule, so the
    explicit-close path leaves no stale handle behind.
    """
    monkeypatch.setattr(rb, "_PAIRING_WINDOW_DURATION_SECONDS", 0.1)

    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    await controller.set_pairing_window(open=True, client="tab-1")
    await controller.set_pairing_window(open=False, client="tab-1")
    # Two events: open + close. After this point, the handle should
    # be None (explicit close cancelled it; no replacement scheduled
    # because the client map is empty).
    assert controller._pairing_window_handle is None
    initial_fire_count = controller._db.bus.fire.call_count
    assert initial_fire_count == 2  # open + explicit close

    # Wait past the original (now-cancelled) deadline. If the handle
    # was leaked, it would fire a second close event here.
    await asyncio.sleep(0.3)

    assert controller._db.bus.fire.call_count == initial_fire_count
    assert controller._pairing_window_handle is None


# ---------------------------------------------------------------------------
# Phase 4a-r1 part 4: peer-link Noise WS dispatch helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_pair_request_creates_pending_row(tmp_path: Path) -> None:
    """First pair_request from a previously-unknown dashboard_id creates PENDING."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\xaa" * 32
    pin = hashlib.sha256(pubkey).hexdigest()

    response = await controller.record_pair_request(
        dashboard_id="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label="alpha",
        peer_ip="192.168.1.10",
    )

    assert response == "pending"
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    [peer] = settings.peers
    assert peer.dashboard_id == "alpha"
    assert peer.status == PeerStatus.PENDING
    assert peer.pin_sha256 == pin
    assert peer.static_x25519_pub == pubkey
    assert peer.label == "alpha"


@pytest.mark.asyncio
async def test_record_pair_request_fires_event(tmp_path: Path) -> None:
    """Creating a PENDING row fires REMOTE_BUILD_PAIR_REQUEST_RECEIVED."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\xbb" * 32
    pin = hashlib.sha256(pubkey).hexdigest()

    await controller.record_pair_request(
        dashboard_id="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label="alpha",
        peer_ip="192.168.1.10",
    )

    fire = controller._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED
    assert payload == {
        "dashboard_id": "alpha",
        "pin_sha256": pin,
        "label": "alpha",
        "peer_ip": "192.168.1.10",
    }


@pytest.mark.asyncio
async def test_record_pair_request_refreshes_existing_pending_row(tmp_path: Path) -> None:
    """Re-pair from same dashboard_id while PENDING refreshes pin / label / paired_at in place."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    initial = _stored_peer(
        dashboard_id="alpha",
        pin_sha256="oldpin",
        static_x25519_pub=b"\x11" * 32,
        label="old",
        paired_at=1.0,
        status=PeerStatus.PENDING,
    )
    await _seed_peer(tmp_path, initial)
    new_pubkey = b"\xcc" * 32
    new_pin = hashlib.sha256(new_pubkey).hexdigest()

    response = await controller.record_pair_request(
        dashboard_id="alpha",
        pin_sha256=new_pin,
        static_x25519_pub=new_pubkey,
        label="renamed",
        peer_ip="10.0.0.1",
    )

    assert response == "pending"
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    [peer] = settings.peers
    assert peer.pin_sha256 == new_pin
    assert peer.static_x25519_pub == new_pubkey
    assert peer.label == "renamed"
    assert peer.paired_at > 1.0
    assert peer.status == PeerStatus.PENDING


@pytest.mark.asyncio
async def test_record_pair_request_already_approved_same_pin_returns_approved(
    tmp_path: Path,
) -> None:
    """
    Pair-request from a still-trusted peer (same pin) returns "approved", no row change.

    Demoting an already-trusted peer back to PENDING on every
    stray pair_request would force the receiver-side user to
    re-approve on every offloader hiccup; pin the
    no-demotion contract for the legitimate case (same dashboard
    id + same pin = same peer, just resending pair_request by
    mistake).
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\x22" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    approved = _stored_peer(
        dashboard_id="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label="alpha",
        paired_at=1.0,
        status=PeerStatus.APPROVED,
    )
    await _seed_peer(tmp_path, approved)

    response = await controller.record_pair_request(
        dashboard_id="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label="renamed-but-ignored",
        peer_ip="10.0.0.1",
    )

    assert response == "approved"
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    [peer] = settings.peers
    assert peer.status == PeerStatus.APPROVED
    assert peer.pin_sha256 == pin
    assert peer.label == "alpha"
    assert peer.paired_at == 1.0
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_record_pair_request_already_approved_different_pin_returns_rejected(
    tmp_path: Path,
) -> None:
    """
    Pair-request from a different pin claiming an APPROVED peer's id returns rejected.

    Either the offloader rotated their X25519 identity (legitimate
    re-pair scenario, but we don't know that and can't safely
    auto-trust) or someone else is presenting a fresh keypair and
    claiming Alice's ``dashboard_id`` (impersonation). Either way:
    refuse, leave the original APPROVED row untouched, don't fire
    an event. The receiver-side user has to click Remove on the
    inbox and re-pair if the rotation is legitimate.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    original_pubkey = b"\x22" * 32
    original_pin = hashlib.sha256(original_pubkey).hexdigest()
    approved = _stored_peer(
        dashboard_id="alpha",
        pin_sha256=original_pin,
        static_x25519_pub=original_pubkey,
        label="alpha",
        paired_at=1.0,
        status=PeerStatus.APPROVED,
    )
    await _seed_peer(tmp_path, approved)

    new_pubkey = b"\x33" * 32
    new_pin = hashlib.sha256(new_pubkey).hexdigest()
    response = await controller.record_pair_request(
        dashboard_id="alpha",
        pin_sha256=new_pin,
        static_x25519_pub=new_pubkey,
        label="renamed",
        peer_ip="10.0.0.1",
    )

    assert response == "rejected"
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    [peer] = settings.peers
    # Original row untouched.
    assert peer.status == PeerStatus.APPROVED
    assert peer.pin_sha256 == original_pin
    assert peer.static_x25519_pub == original_pubkey
    assert peer.label == "alpha"
    assert peer.paired_at == 1.0
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_peer_for_session_approved_returns_ok(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\xdd" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    await _seed_peer(
        tmp_path,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            status=PeerStatus.APPROVED,
        ),
    )

    response = await controller.lookup_peer_for_session(dashboard_id="alpha", pin_sha256=pin)

    assert response == "ok"


@pytest.mark.asyncio
async def test_lookup_peer_for_session_pending_returns_pending(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\xee" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    await _seed_peer(
        tmp_path,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            status=PeerStatus.PENDING,
        ),
    )

    response = await controller.lookup_peer_for_session(dashboard_id="alpha", pin_sha256=pin)

    assert response == "pending"


@pytest.mark.asyncio
async def test_lookup_peer_for_session_unknown_returns_rejected(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    response = await controller.lookup_peer_for_session(dashboard_id="ghost", pin_sha256="anything")

    assert response == "rejected"


@pytest.mark.asyncio
async def test_lookup_peer_for_session_pin_mismatch_returns_rejected(tmp_path: Path) -> None:
    """
    Stored row exists but pin doesn't match the handshake's pubkey hash.

    The Noise handshake authenticates the pubkey cryptographically;
    if the handshake's pin doesn't match the stored value, the
    offloader is presenting a different identity than the row was
    paired against. Could be: rotation under us, stolen
    dashboard_id, or fresh attacker. Either way: don't connect.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    stored_pubkey = b"\xff" * 32
    stored_pin = hashlib.sha256(stored_pubkey).hexdigest()
    await _seed_peer(
        tmp_path,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=stored_pin,
            static_x25519_pub=stored_pubkey,
            status=PeerStatus.APPROVED,
        ),
    )

    response = await controller.lookup_peer_for_session(
        dashboard_id="alpha", pin_sha256="differentpin" * 4
    )

    assert response == "rejected"


@pytest.mark.asyncio
async def test_lookup_peer_for_status_mirrors_session_but_uses_approved_string(
    tmp_path: Path,
) -> None:
    """``pair_status`` returns "approved" where ``peer_link`` returns "ok"; rest is the same."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\x44" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    await _seed_peer(
        tmp_path,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            status=PeerStatus.APPROVED,
        ),
    )

    status_response = await controller.lookup_peer_for_status(dashboard_id="alpha", pin_sha256=pin)
    session_response = await controller.lookup_peer_for_session(
        dashboard_id="alpha", pin_sha256=pin
    )

    assert status_response == "approved"
    assert session_response == "ok"


@pytest.mark.asyncio
async def test_lookup_peer_for_status_unknown_returns_rejected(tmp_path: Path) -> None:
    """A removed/rejected peer (or one that never existed) returns rejected."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    response = await controller.lookup_peer_for_status(dashboard_id="ghost", pin_sha256="pin")

    assert response == "rejected"
