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
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zeroconf import ServiceStateChange

from esphome_device_builder.controllers.config import (
    load_remote_build_settings,
    remote_build_settings_transaction,
)
from esphome_device_builder.controllers.remote_build import (
    _MAX_TOKENS,
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
    ManualHost,
    RemoteBuildPeer,
    RemoteBuildPeerSource,
    RemoteBuildSettingsView,
    StoredToken,
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
async def test_list_hosts_returns_snapshot_of_peers(tmp_path: Any) -> None:
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
async def test_list_hosts_empty_when_no_peers(tmp_path: Any) -> None:
    controller = _make_controller(config_dir=tmp_path)
    assert await controller.list_hosts() == []


@pytest.mark.asyncio
async def test_get_settings_defaults_when_unset(tmp_path: Any) -> None:
    """A fresh dashboard with no metadata returns ``enabled=False``."""
    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.get_settings()
    assert settings == RemoteBuildSettingsView(enabled=False)


@pytest.mark.asyncio
async def test_set_settings_round_trips(tmp_path: Any) -> None:
    """Setting ``enabled=True`` persists and is read back by ``get_settings``."""
    controller = _make_controller(config_dir=tmp_path)
    written = await controller.set_settings(enabled=True)
    assert written == RemoteBuildSettingsView(enabled=True)
    read = await controller.get_settings()
    assert read == RemoteBuildSettingsView(enabled=True)


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
async def test_add_manual_host_persists_and_returns_settings(tmp_path: Any) -> None:
    """Happy path: a unique entry is appended and the settings round-trip."""
    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.add_manual_host(hostname="desktop.local", port=6052)
    assert settings.manual_hosts == [ManualHost(hostname="desktop.local", port=6052)]
    # Round-trip: get_settings reflects the persisted state.
    reread = await controller.get_settings()
    assert reread.manual_hosts == settings.manual_hosts


@pytest.mark.asyncio
async def test_add_manual_host_rejects_duplicate(tmp_path: Any) -> None:
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
async def test_add_manual_host_normalises_case_for_dedup(tmp_path: Any) -> None:
    """``Desktop.Local`` and ``desktop.local`` are the same entry."""
    controller = _make_controller(config_dir=tmp_path)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    with pytest.raises(CommandError):
        await controller.add_manual_host(hostname="Desktop.Local", port=6052)


@pytest.mark.asyncio
async def test_add_manual_host_keeps_enabled_intact(tmp_path: Any) -> None:
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
async def test_remove_manual_host_drops_entry(tmp_path: Any) -> None:
    """Happy path: a registered entry is removed."""
    controller = _make_controller(config_dir=tmp_path)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    await controller.add_manual_host(hostname="laptop.local", port=6052)
    settings = await controller.remove_manual_host(hostname="desktop.local", port=6052)
    assert settings.manual_hosts == [ManualHost(hostname="laptop.local", port=6052)]


@pytest.mark.asyncio
async def test_remove_manual_host_rejects_unknown(tmp_path: Any) -> None:
    """``NOT_FOUND`` for a host that was never registered."""
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.remove_manual_host(hostname="ghost.local", port=6052)
    assert exc.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_remove_manual_host_normalises_case(tmp_path: Any) -> None:
    """``Desktop.Local`` removes a stored ``desktop.local`` entry."""
    controller = _make_controller(config_dir=tmp_path)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    settings = await controller.remove_manual_host(hostname="Desktop.Local", port=6052)
    assert settings.manual_hosts == []


@pytest.mark.asyncio
async def test_set_settings_preserves_manual_hosts(tmp_path: Any) -> None:
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
async def test_list_hosts_merges_mdns_and_manual(tmp_path: Any) -> None:
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
async def test_add_manual_host_rejects_invalid_port(tmp_path: Any) -> None:
    """Out-of-range port doesn't slip through."""
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.add_manual_host(hostname="desktop.local", port=0)
    assert exc.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_add_manual_host_rejects_blank_hostname(tmp_path: Any) -> None:
    """Empty / whitespace hostname doesn't slip through."""
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.add_manual_host(hostname="   ", port=6052)
    assert exc.value.code == ErrorCode.INVALID_ARGS


# ---------------------------------------------------------------------------
# Token CRUD (phase 3b1)
# ---------------------------------------------------------------------------


def _split_bearer(bearer: str) -> tuple[str, str]:
    """Return ``(token_id, secret)`` from a wire bearer."""
    token_id, secret = bearer.split(".", 1)
    return token_id, secret


@pytest.mark.asyncio
async def test_add_token_returns_cleartext_bearer_once(tmp_path: Any) -> None:
    """
    ``add_token`` flashes the cleartext bearer through exactly once.

    Also pins the wire form (``{token_id}.{secret}``) and that
    both halves are high-entropy distinct values across calls;
    a refactor that swapped to a counter or a label-derived id
    would fail here.
    """
    controller = _make_controller(config_dir=tmp_path)
    first = await controller.add_token(label="Green dashboard")
    second = await controller.add_token(label="Green dashboard")

    assert first.label == "Green dashboard"
    assert first.created_at > 0
    # Wire form: ``{token_id}.{secret}`` with both halves substantial.
    fid, fsecret = _split_bearer(first.bearer)
    sid, ssecret = _split_bearer(second.bearer)
    assert fid == first.token_id
    assert sid == second.token_id
    # Two calls give distinct high-entropy values everywhere.
    assert fid != sid
    assert fsecret != ssecret
    assert len(fid) >= 8 and len(fsecret) >= 40


@pytest.mark.asyncio
async def test_add_token_persists_only_hashed_secret(tmp_path: Any) -> None:
    """
    The on-disk row carries SHA-256 of the secret only; never the cleartext.

    Inspects the on-disk shape via ``load_remote_build_settings``
    (storage form, ``StoredToken`` rows with ``secret_sha256``)
    rather than ``get_settings`` (wire form,
    :class:`RemoteBuildSettingsView` with hashes stripped). The
    storage form is the place to assert the hash is the only
    representation that lands on disk.
    """
    controller = _make_controller(config_dir=tmp_path)
    result = await controller.add_token(label="Green")
    _, secret = _split_bearer(result.bearer)

    loop = asyncio.get_running_loop()
    on_disk = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    assert len(on_disk.tokens) == 1
    stored = on_disk.tokens[0]
    assert stored.token_id == result.token_id
    assert stored.secret_sha256 == hashlib.sha256(secret.encode("ascii")).hexdigest()
    assert secret not in stored.secret_sha256
    assert stored.bound_dashboard_id is None


@pytest.mark.asyncio
async def test_settings_responses_never_carry_secret_hash(tmp_path: Any) -> None:
    """
    Every WS command that returns settings projects tokens to ``TokenSummary``.

    ``RemoteBuildSettings`` is the storage shape; ``RemoteBuildSettingsView``
    is the wire shape. A regression that returned the storage shape
    over the WS would leak ``secret_sha256`` to the frontend on
    every CRUD response (set_settings, add_manual_host,
    remove_manual_host, remove_token, get_settings). Pin that
    none of the wire returns expose the field.
    """
    controller = _make_controller(config_dir=tmp_path)
    issued = await controller.add_token(label="Green")

    # Every method that returns settings to the wire.
    responses = [
        await controller.get_settings(),
        await controller.set_settings(enabled=True),
        await controller.add_manual_host(hostname="desktop.local", port=6052),
        await controller.remove_manual_host(hostname="desktop.local", port=6052),
        await controller.remove_token(token_id=issued.token_id),
    ]
    for response in responses:
        # ``RemoteBuildSettingsView.tokens`` is ``list[TokenSummary]``;
        # neither the dataclass nor the dict-form should carry
        # ``secret_sha256``.
        for entry in response.tokens:
            assert not hasattr(entry, "secret_sha256")
        assert "secret_sha256" not in response.to_dict()["tokens"].__repr__()


@pytest.mark.asyncio
async def test_list_tokens_omits_secret_hash(tmp_path: Any) -> None:
    """The ``list_tokens`` projection drops ``secret_sha256`` and allows dup labels."""
    controller = _make_controller(config_dir=tmp_path)
    first = await controller.add_token(label="phone")
    second = await controller.add_token(label="phone")
    assert first.token_id != second.token_id  # token_id is the unique key

    summaries = await controller.list_tokens()
    assert [s.label for s in summaries] == ["phone", "phone"]
    for summary in summaries:
        assert not hasattr(summary, "secret_sha256")
        assert summary.bound_dashboard_id is None


@pytest.mark.parametrize(
    ("label", "expected_code"),
    [
        pytest.param("", ErrorCode.INVALID_ARGS, id="empty"),
        pytest.param("   ", ErrorCode.INVALID_ARGS, id="whitespace-only"),
        pytest.param("\t\n", ErrorCode.INVALID_ARGS, id="tabs-newlines"),
        pytest.param("x" * 200, ErrorCode.INVALID_ARGS, id="overlong"),
        pytest.param(123, ErrorCode.INVALID_ARGS, id="non-string-int"),
        pytest.param(None, ErrorCode.INVALID_ARGS, id="non-string-none"),
    ],
)
@pytest.mark.asyncio
async def test_add_token_rejects_invalid_label(
    tmp_path: Any, label: object, expected_code: ErrorCode
) -> None:
    """Empty / overlong / non-string labels don't slip through."""
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.add_token(label=label)  # type: ignore[arg-type]
    assert exc.value.code == expected_code


@pytest.mark.asyncio
async def test_add_token_keeps_other_settings_intact(tmp_path: Any) -> None:
    """Issuing a token doesn't reset ``enabled`` or ``manual_hosts``."""
    controller = _make_controller(config_dir=tmp_path)
    await controller.set_settings(enabled=True)
    await controller.add_manual_host(hostname="desktop.local", port=6052)
    await controller.add_token(label="Green")

    settings = await controller.get_settings()
    assert settings.enabled is True
    assert settings.manual_hosts == [ManualHost(hostname="desktop.local", port=6052)]
    assert len(settings.tokens) == 1


@pytest.mark.asyncio
async def test_remove_token_drops_only_target(tmp_path: Any) -> None:
    """Removing one token leaves the rest of the list intact."""
    controller = _make_controller(config_dir=tmp_path)
    keep_a = await controller.add_token(label="Green")
    target = await controller.add_token(label="Laptop")
    keep_b = await controller.add_token(label="Phone")

    settings = await controller.remove_token(token_id=target.token_id)
    assert [t.token_id for t in settings.tokens] == [keep_a.token_id, keep_b.token_id]


@pytest.mark.parametrize(
    ("token_id", "expected_code"),
    [
        pytest.param("ghost123", ErrorCode.NOT_FOUND, id="unknown-id"),
        pytest.param("   ", ErrorCode.INVALID_ARGS, id="blank-id"),
        pytest.param("", ErrorCode.INVALID_ARGS, id="empty-id"),
        pytest.param(123, ErrorCode.INVALID_ARGS, id="non-string-int"),
        pytest.param(None, ErrorCode.INVALID_ARGS, id="non-string-none"),
        pytest.param("not!base64", ErrorCode.INVALID_ARGS, id="non-base64url-chars"),
        pytest.param("a" * 65, ErrorCode.INVALID_ARGS, id="overlong"),
    ],
)
@pytest.mark.asyncio
async def test_remove_token_rejects_invalid(
    tmp_path: Any, token_id: object, expected_code: ErrorCode
) -> None:
    """Unknown / blank / empty / non-string / malformed ``token_id`` is rejected."""
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.remove_token(token_id=token_id)  # type: ignore[arg-type]
    assert exc.value.code == expected_code


@pytest.mark.asyncio
async def test_remove_token_rejects_full_bearer_without_echoing_secret(
    tmp_path: Any,
) -> None:
    """
    Passing the full bearer to ``remove_token`` is rejected before logging.

    The bearer wire form is ``{token_id}.{secret}``. If a frontend
    bug or operator typo passes the whole bearer instead of the id
    half, the cleartext secret would land in the error message and
    propagate over the WS into browser DevTools / frontend
    telemetry. The validator rejects on ``.`` and the rejection
    message must NOT echo the secret back.
    """
    controller = _make_controller(config_dir=tmp_path)
    issued = await controller.add_token(label="Green")
    full_bearer = issued.bearer  # ``{token_id}.{secret}``
    secret = full_bearer.split(".", 1)[1]

    with pytest.raises(CommandError) as exc:
        await controller.remove_token(token_id=full_bearer)
    assert exc.value.code == ErrorCode.INVALID_ARGS
    # The whole point of the check: the secret half must not appear
    # anywhere in the error message.
    assert secret not in str(exc.value)
    assert full_bearer not in str(exc.value)


@pytest.mark.asyncio
async def test_remove_token_not_found_does_not_echo_id(tmp_path: Any) -> None:
    """The ``NOT_FOUND`` message doesn't echo the user-supplied ``token_id``."""
    controller = _make_controller(config_dir=tmp_path)
    suspicious = "lookslike-id-but-isnt"
    with pytest.raises(CommandError) as exc:
        await controller.remove_token(token_id=suspicious)
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert suspicious not in str(exc.value)


@pytest.mark.asyncio
async def test_add_token_rejects_when_at_capacity(tmp_path: Any) -> None:
    """
    ``add_token`` refuses once the receiver hits the soft cap.

    Defends against a runaway frontend looping ``add_token`` and
    growing the metadata sidecar unboundedly. Pre-seed the disk
    state with the cap's worth of tokens so the test doesn't have
    to actually mint 100 ed25519-strength secrets. Seed via
    ``run_in_executor`` because the transaction does sync I/O
    that blockbuster (Linux CI) flags from inside an async test.
    """

    def _seed_at_capacity() -> None:
        with remote_build_settings_transaction(tmp_path) as settings:
            for i in range(_MAX_TOKENS):
                settings.tokens.append(
                    StoredToken(
                        token_id=f"id{i:04d}",
                        label=f"pre-{i}",
                        secret_sha256="0" * 64,
                        created_at=0.0,
                    )
                )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _seed_at_capacity)

    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.add_token(label="overflow")
    assert exc.value.code == ErrorCode.INVALID_ARGS
    # Cap message names the limit so the operator can act.
    assert str(_MAX_TOKENS) in str(exc.value)


@pytest.mark.asyncio
async def test_load_remote_build_settings_falls_back_on_unrecoverable_blob(
    tmp_path: Any,
) -> None:
    """
    A blob that fails to deserialise even after token-row cleaning resets to defaults.

    Token-row tolerance handles the common case (one bad token
    didn't disconnect every peer); but a wholly malformed blob
    (e.g. ``manual_hosts`` set to a non-list, ``enabled`` set to
    a list, etc.) still falls back to the empty defaults rather
    than crashing dashboard startup. Pin the rescue branch.
    """
    await _seed_metadata(
        tmp_path,
        {
            # Type errors mashumaro rejects: ``manual_hosts`` must
            # be a list, ``enabled`` must be a bool. The
            # token-row pre-clean can't save this.
            "enabled": "definitely-not-a-bool",
            "manual_hosts": "definitely-not-a-list",
        },
    )
    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.get_settings()
    # All fields back to defaults; the dashboard didn't crash.
    assert settings.enabled is False
    assert settings.manual_hosts == []
    assert settings.tokens == []


@pytest.mark.asyncio
async def test_load_remote_build_settings_drops_malformed_token_rows(
    tmp_path: Any,
) -> None:
    """
    One corrupt token row doesn't blank the rest of the receiver's view.

    Mirrors the labels-row-by-row contract. Without it, an operator
    who hand-edited the sidecar (or hit an in-flight schema change)
    would lose every paired peer until manual repair. The good rows
    must still load.
    """
    await _seed_metadata(
        tmp_path,
        {
            "enabled": True,
            "tokens": [
                {
                    "token_id": "good1",
                    "label": "Green",
                    "secret_sha256": "a" * 64,
                    "created_at": 1.0,
                    "bound_dashboard_id": None,
                },
                # Missing required ``secret_sha256`` field.
                {"token_id": "bad1", "label": "Broken", "created_at": 2.0},
                {
                    "token_id": "good2",
                    "label": "Laptop",
                    "secret_sha256": "b" * 64,
                    "created_at": 3.0,
                    "bound_dashboard_id": None,
                },
            ],
        },
    )

    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.get_settings()
    assert settings.enabled is True
    assert [t.token_id for t in settings.tokens] == ["good1", "good2"]


@pytest.mark.asyncio
async def test_decode_tokens_skips_non_dict_entries(tmp_path: Any) -> None:
    """
    Non-dict entries in the on-disk ``tokens`` list are skipped silently.

    A hand-edited (or just type-confused) sidecar might land a
    string or null in the tokens list. The decoder skips those
    without raising and without invoking ``StoredToken.from_dict``
    (which would raise on a non-dict). Good rows still load.
    """
    await _seed_metadata(
        tmp_path,
        {
            "tokens": [
                {
                    "token_id": "good1",
                    "label": "Green",
                    "secret_sha256": "a" * 64,
                    "created_at": 1.0,
                    "bound_dashboard_id": None,
                },
                "not-a-dict-at-all",  # noqa: type-confused row
                42,
                None,
            ],
        },
    )

    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.get_settings()
    assert [t.token_id for t in settings.tokens] == ["good1"]


@pytest.mark.asyncio
async def test_decode_tokens_redacts_credential_material_from_logs(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """
    The malformed-row debug log doesn't carry credential-adjacent fields.

    A hand-edited sidecar could land a cleartext secret in the
    wrong field by mistake; the row-skip log MUST NOT echo the
    full entry dict back, only the non-sensitive ``token_id``.
    Captures ``%r``-dump regressions before they ship.
    """
    cleartext_marker = "PASTED-CLEARTEXT-SECRET-DO-NOT-LOG"
    await _seed_metadata(
        tmp_path,
        {
            "tokens": [
                {
                    "token_id": "rowid",
                    "label": "Broken",
                    # Missing ``secret_sha256`` -> from_dict raises.
                    # Add a fake field with the canary string so a
                    # ``%r``-dump of the whole entry would reveal it.
                    "leaked_field": cleartext_marker,
                },
            ],
        },
    )

    controller = _make_controller(config_dir=tmp_path)
    with caplog.at_level("DEBUG", logger="esphome_device_builder.controllers.config"):
        await controller.get_settings()

    skip_logs = [r for r in caplog.records if "Skipping malformed token entry" in r.getMessage()]
    assert skip_logs, "expected at least one skip log"
    for record in skip_logs:
        assert cleartext_marker not in record.getMessage()
    # The token_id (public lookup key) is fine to log.
    assert any("rowid" in r.getMessage() for r in skip_logs)


@pytest.mark.asyncio
async def test_loads_legacy_metadata_without_tokens_key(tmp_path: Any) -> None:
    """
    Phase-2/2b on-disk JSON without a ``tokens`` key still loads cleanly.

    Mashumaro + the ``default_factory=list`` on ``RemoteBuildSettings.tokens``
    is what bridges the version skew. A future refactor that
    accidentally tightens ``from_dict`` would break this contract
    silently — every existing install would lose its
    ``manual_hosts`` and ``enabled`` on first boot. Pin it.
    """
    await _seed_metadata(
        tmp_path,
        {
            "enabled": True,
            "manual_hosts": [{"hostname": "desktop.local", "port": 6052}],
            # Note: no ``tokens`` key — what phase 2b shipped.
        },
    )
    controller = _make_controller(config_dir=tmp_path)
    settings = await controller.get_settings()
    assert settings.enabled is True
    assert settings.manual_hosts == [ManualHost(hostname="desktop.local", port=6052)]
    assert settings.tokens == []
