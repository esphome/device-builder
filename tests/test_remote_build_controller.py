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

from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build import controller as rb
from esphome_device_builder.controllers.remote_build.controller import (
    _decode_pairings,
    _decode_txt_value,
    _encode_pairings,
    _enforce_pin_match,
    _intent_response_to_command_error,
    _pairing_summary,
    _PairLabelField,
    _peer_from_service_info,
    _validate_hostname,
    _validate_pair_label,
    _validate_pin_sha256,
    _validate_port,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.dashboard_advertise import SERVICE_TYPE
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import (
    ErrorCode,
    EventType,
    IdentityView,
    IntentResponse,
    OffloaderRemoteBuildSettings,
    PairingSummary,
    PeerStatus,
    RemoteBuildPeer,
    RemoteBuildPeerSource,
    RemoteBuildSettingsView,
    StoredPairing,
    StoredPeer,
)

from .conftest import make_remote_build_controller

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


def _make_controller(*, config_dir: Path, real_bus: bool = False) -> RemoteBuildController:
    # The controller's ``__init__`` constructs a per-file
    # ``Store`` keyed off ``config_dir / ".offloader_pairings.json"``,
    # so callers must thread a real ``Path`` through (typically
    # pytest's ``tmp_path``). Mocking it would land the store at
    # ``MagicMock() / "..."`` and trip ``__truediv__`` somewhere
    # downstream; an explicit signature beats the silent failure
    # mode.
    #
    # Long-poll tests for ``lookup_peer_for_status`` exercise the
    # bus.listening machinery for real (a MagicMock bus would
    # never deliver events to the listener and the long-poll
    # would only ever take the timeout fallback); they pass
    # ``real_bus=True``.
    bus = EventBus() if real_bus else None
    controller = make_remote_build_controller(config_dir=config_dir, bus=bus)
    # ``set_settings`` calls ``apply_remote_build_enabled`` to
    # live-rebind the listener; in-process tests don't exercise
    # the bind path so a no-op AsyncMock is sufficient. Patched
    # on the per-test stub-DB rather than baked into the shared
    # helper because only this file's tests touch ``set_settings``.
    controller._db.apply_remote_build_enabled = AsyncMock(return_value=False)
    return controller


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


def test_on_service_state_change_filters_own_advertise(tmp_path: Path) -> None:
    """Our own service-instance name never lands in ``_peers``."""
    controller = _make_controller(config_dir=tmp_path)
    controller._own_instance_name = f"self.{SERVICE_TYPE}"
    zeroconf = MagicMock()
    controller._on_service_state_change(
        zeroconf, SERVICE_TYPE, f"self.{SERVICE_TYPE}", ServiceStateChange.Added
    )
    assert controller._peers == {}


def test_on_service_state_change_removed_drops_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``Removed`` event clears the peer entry and fires ``REMOTE_BUILD_HOST_REMOVED``."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
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
    # Event keys on the wire-friendly label (matches
    # ``RemoteBuildPeer.name``), not the FQDN dict key.
    controller._db.bus.fire.assert_called_once_with(
        EventType.REMOTE_BUILD_HOST_REMOVED,
        {"name": "desktop"},
    )


def test_on_service_state_change_removed_unknown_does_not_fire(tmp_path: Path) -> None:
    """A ``Removed`` for a peer we never indexed is a silent no-op.

    Pin the predicate that the dict-mutation gates the event fire
    — without it, a benign zeroconf storm could spam
    ``REMOTE_BUILD_HOST_REMOVED`` for names the frontend never
    saw, and the frontend would have to defensively no-op the
    delete.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    controller._on_service_state_change(
        MagicMock(), SERVICE_TYPE, f"ghost.{SERVICE_TYPE}", ServiceStateChange.Removed
    )
    controller._db.bus.fire.assert_not_called()


def test_on_service_state_change_uses_cache_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-hit upserts the peer synchronously and fires ``REMOTE_BUILD_HOST_ADDED``."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    fake_info = _fake_service_info(name="desktop")
    fake_info.load_from_cache = MagicMock(return_value=True)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.AsyncServiceInfo",
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
    # Event fired with the full peer projection so the frontend
    # can render the row from the event alone.
    controller._db.bus.fire.assert_called_once()
    event_type, payload = controller._db.bus.fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_HOST_ADDED
    assert payload["name"] == "desktop"
    assert payload["hostname"]
    assert payload["port"] == 6052
    assert payload["source"] == "mdns"


# ---------------------------------------------------------------------------
# WS commands
# ---------------------------------------------------------------------------


def test_hosts_snapshot_returns_mdns_peers(tmp_path: Path) -> None:
    """``hosts_snapshot`` is a sync read of the in-RAM ``_peers`` dict."""
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
    result = controller.hosts_snapshot()
    assert {peer.name for peer in result} == {"desktop", "laptop"}
    assert all(peer.source == RemoteBuildPeerSource.MDNS for peer in result)


def test_hosts_snapshot_empty_when_no_peers(tmp_path: Path) -> None:
    """Empty ``_peers`` dict snapshots to an empty list, not an error."""
    controller = _make_controller(config_dir=tmp_path)
    assert controller.hosts_snapshot() == []


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
        "esphome_device_builder.controllers.remote_build.controller.AsyncServiceBrowser",
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
        "esphome_device_builder.controllers.remote_build.controller.AsyncServiceBrowser",
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
        "esphome_device_builder.controllers.remote_build.controller.AsyncServiceBrowser",
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
        "esphome_device_builder.controllers.remote_build.controller.AsyncServiceBrowser",
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
        "esphome_device_builder.controllers.remote_build.controller.AsyncServiceBrowser",
        MagicMock(return_value=fake_browser),
    )
    controller = _make_controller(config_dir=tmp_path)
    controller._db.devices.zeroconf = MagicMock()
    await controller.start()
    await controller.stop()  # must not raise
    assert controller._browser is None


@pytest.mark.asyncio
async def test_on_service_state_change_spawns_resolve_task_on_cache_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-miss queues the async resolve task; success fires ``REMOTE_BUILD_HOST_ADDED``."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    fake_info = _fake_service_info(name="desktop")
    fake_info.load_from_cache = MagicMock(return_value=False)
    fake_info.async_request = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.AsyncServiceInfo",
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
    # Event fired after the async resolve succeeded.
    controller._db.bus.fire.assert_called_once()
    event_type, _ = controller._db.bus.fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_HOST_ADDED


@pytest.mark.asyncio
async def test_resolve_and_apply_swallows_errors(tmp_path: Path) -> None:
    """A resolve-side exception leaves the peer map untouched."""
    controller = _make_controller(config_dir=tmp_path)
    fake_info = _fake_service_info(name="desktop")
    fake_info.async_request = AsyncMock(side_effect=RuntimeError("network down"))
    await controller._resolve_and_apply(MagicMock(), fake_info, f"desktop.{SERVICE_TYPE}")
    assert controller._peers == {}


@pytest.mark.asyncio
async def test_resolve_and_apply_skips_when_resolution_returns_false(tmp_path: Path) -> None:
    """An ``async_request`` that returns ``False`` (timeout) doesn't add a peer."""
    controller = _make_controller(config_dir=tmp_path)
    fake_info = _fake_service_info(name="desktop")
    fake_info.async_request = AsyncMock(return_value=False)
    await controller._resolve_and_apply(MagicMock(), fake_info, f"desktop.{SERVICE_TYPE}")
    assert controller._peers == {}


@pytest.mark.asyncio
async def test_stop_drains_resolve_tasks(tmp_path: Path) -> None:
    """In-flight resolve tasks are cancelled and the set is cleared."""
    controller = _make_controller(config_dir=tmp_path)
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
    peer_ip: str = "192.168.1.10",
) -> StoredPeer:
    """Construct a ``StoredPeer`` with sensible defaults for tests.

    All ``StoredPeer`` instances are implicitly APPROVED in the
    storage model — PENDING peers live in the controller's
    in-memory ``_pending_peers`` dict, not in the dataclass.
    Tests that need a PENDING entry build a ``StoredPeer`` with
    this helper and add it via :func:`_seed_pending_peer`; tests
    that need an APPROVED entry seed it via :func:`_seed_peer`.
    """
    pub = static_x25519_pub if static_x25519_pub is not None else _secrets.token_bytes(32)
    pin = pin_sha256 if pin_sha256 is not None else hashlib.sha256(pub).hexdigest()
    return StoredPeer(
        dashboard_id=dashboard_id,
        pin_sha256=pin,
        static_x25519_pub=pub,
        label=label,
        paired_at=paired_at,
        peer_ip=peer_ip,
    )


def _seed_peer(controller: RemoteBuildController, peer: StoredPeer) -> None:
    """Insert *peer* into the controller's RAM-canonical APPROVED dict.

    APPROVED peers are RAM-canonical at runtime (mirror of the
    offloader-side ``_pairings`` shape); the per-file
    :class:`~helpers.storage.Store` at
    ``<config_dir>/.receiver_peers.json`` is just persistence
    across restarts. Tests don't need to round-trip through disk
    — populating ``_approved_peers`` directly mirrors what
    :meth:`start` would do after an :meth:`approve_peer` flow,
    and lets the test stay sync.
    """
    controller._approved_peers[peer.dashboard_id] = peer


def _seed_pending_peer(controller: RemoteBuildController, peer: StoredPeer) -> None:
    """Add *peer* to the controller's in-memory pending dict.

    Mirrors what ``record_pair_request`` does for a fresh
    pair_request inside an open pairing window — sets up a
    ``StoredPeer`` instance keyed on ``dashboard_id``. Use this
    in place of :func:`_seed_peer` for tests that exercise the
    PENDING path; the persistent list stays APPROVED-only.
    """
    controller._pending_peers[peer.dashboard_id] = peer


def test_peers_snapshot_returns_empty_when_none_stored(tmp_path: Path) -> None:
    """Snapshot on a fresh dashboard returns an empty list, not an error."""
    controller = _make_controller(config_dir=tmp_path)
    assert controller.peers_snapshot() == []


def test_peers_snapshot_returns_summary_for_each_row(tmp_path: Path) -> None:
    """``peers_snapshot`` merges in-memory PENDING + APPROVED rows from RAM."""
    controller = _make_controller(config_dir=tmp_path)
    pending = _stored_peer(dashboard_id="pending")
    approved = _stored_peer(dashboard_id="approved")
    _seed_pending_peer(controller, pending)
    _seed_peer(controller, approved)

    rows = controller.peers_snapshot()

    assert {row.dashboard_id for row in rows} == {"pending", "approved"}
    statuses = {row.dashboard_id: row.status for row in rows}
    assert statuses == {"pending": PeerStatus.PENDING, "approved": PeerStatus.APPROVED}


def test_peers_snapshot_drops_static_x25519_pub_from_wire(tmp_path: Path) -> None:
    """The wire summary must not expose raw ``static_x25519_pub`` bytes."""
    controller = _make_controller(config_dir=tmp_path)
    _seed_peer(controller, _stored_peer(static_x25519_pub=b"\xaa" * 32))

    [row] = controller.peers_snapshot()

    serialised = row.to_dict()
    assert "static_x25519_pub" not in serialised
    assert serialised["pin_sha256"]  # the wire-friendly form is present


def test_peers_snapshot_marks_approved_with_active_session_connected(
    tmp_path: Path,
) -> None:
    """An APPROVED row with a registered peer-link session reports ``connected=True``.

    The frontend's "Paired senders" list reads ``connected``
    to render an online/offline indicator. Pin the snapshot
    semantic so a future refactor that splits the session
    registry from the peer dict can't silently drop the
    membership read.
    """
    controller = _make_controller(config_dir=tmp_path)
    _seed_peer(controller, _stored_peer(dashboard_id="alpha"))
    _seed_peer(controller, _stored_peer(dashboard_id="beta"))
    # Stub a session for ``alpha`` only; ``beta`` is approved
    # but offline.
    session = MagicMock()
    session.dashboard_id = "alpha"
    controller._peer_link_sessions["alpha"] = session

    rows = {row.dashboard_id: row for row in controller.peers_snapshot()}

    assert rows["alpha"].connected is True
    assert rows["beta"].connected is False


def test_peers_snapshot_pending_row_is_never_connected(tmp_path: Path) -> None:
    """PENDING rows project as ``connected=False`` regardless of session state.

    Peer-link is gated on APPROVED status (the receiver's
    ``lookup_peer_for_session`` refuses non-APPROVED rows), so
    a registered session with the same dashboard_id as a
    PENDING entry shouldn't surface as ``connected=True``. The
    invariant is the dispatch gate's responsibility, not the
    snapshot's, but this test pins the structural default in
    case a future code path legitimately registers a session
    for a non-APPROVED row (it'd need to come back and lift
    the hardcoded ``False``).
    """
    controller = _make_controller(config_dir=tmp_path)
    _seed_pending_peer(controller, _stored_peer(dashboard_id="pending"))
    session = MagicMock()
    session.dashboard_id = "pending"
    controller._peer_link_sessions["pending"] = session

    [row] = controller.peers_snapshot()

    assert row.dashboard_id == "pending"
    assert row.status is PeerStatus.PENDING
    assert row.connected is False


def test_peers_snapshot_carries_peer_ip(tmp_path: Path) -> None:
    """``peer_ip`` flows from the stored row through the wire summary.

    The receiver Settings inbox uses ``peer_ip`` as a clone-risk
    sanity-check (operator can verify the request came from the
    expected host). Pinning the projection here so a future
    refactor can't silently drop it from the wire shape — that
    would degrade a snapshot-loaded PENDING row to "no IP, can't
    sanity-check" without admin noticing.
    """
    controller = _make_controller(config_dir=tmp_path)
    _seed_pending_peer(
        controller,
        _stored_peer(dashboard_id="pending", peer_ip="192.168.1.55"),
    )
    _seed_peer(
        controller,
        _stored_peer(dashboard_id="approved", peer_ip="10.0.0.7"),
    )

    rows = {row.dashboard_id: row for row in controller.peers_snapshot()}

    assert rows["pending"].peer_ip == "192.168.1.55"
    assert rows["approved"].peer_ip == "10.0.0.7"


def test_stored_peer_refresh_from_pair_request_updates_all_documented_fields() -> None:
    """``StoredPeer.refresh_from_pair_request`` updates exactly the fields its docstring claims.

    The helper documents the "what changes on re-pair" contract:
    pin / pubkey / label / paired_at / peer_ip refresh in place
    against an existing row, while ``dashboard_id`` (the row's
    primary key) and persisted ``status`` are left alone. Pin
    that contract here so a future refactor can't silently drop
    or add a field without the docstring keeping up — the helper
    is the seam future re-pair callers will reach for, and a
    silent shape drift would land as a security-relevant bug
    (e.g. failing to refresh ``peer_ip`` on a DHCP-renewed
    offloader leaves the inbox showing a stale source IP).
    """
    peer = StoredPeer(
        dashboard_id="alpha",
        pin_sha256="oldpin",
        static_x25519_pub=b"\x11" * 32,
        label="old",
        paired_at=1.0,
        peer_ip="192.168.1.10",
    )

    new_pubkey = b"\x22" * 32
    peer.refresh_from_pair_request(
        pin_sha256="newpin",
        static_x25519_pub=new_pubkey,
        label="renamed",
        paired_at=2.0,
        peer_ip="10.0.0.7",
    )

    # All documented fields refreshed.
    assert peer.pin_sha256 == "newpin"
    assert peer.static_x25519_pub == new_pubkey
    assert peer.label == "renamed"
    assert peer.paired_at == 2.0
    assert peer.peer_ip == "10.0.0.7"
    # ``dashboard_id`` is the primary key — intentionally left
    # alone; mutating it would orphan the dict entry under the
    # caller.
    assert peer.dashboard_id == "alpha"


@pytest.mark.asyncio
async def test_start_seeds_approved_peers_dict_from_disk(tmp_path: Path) -> None:
    """``start()`` loads APPROVED peers off disk into ``_approved_peers``.

    Pre-seed the per-file ``Store`` with a row, instantiate a
    fresh controller pointing at the same dir, call ``start()``,
    and assert the dict is populated. Pins the cold-start
    contract — APPROVED rows survive a controller restart so the
    receiver-side admin doesn't have to re-approve every offloader
    on every dashboard bounce. Mirrors the offloader-side
    ``test_start_seeds_pairings_dict_from_disk`` shape.
    """
    seeder = _make_controller(config_dir=tmp_path)
    seeder._db.bus = MagicMock()
    seeder._db.devices = None  # short-circuit the post-load branches.
    pubkey = b"\xee" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    seeder._approved_peers["alpha"] = _stored_peer(
        dashboard_id="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        peer_ip="172.16.5.42",
    )
    # Force-flush the debounced save through the same shutdown
    # callback path production uses; the offloader-side test
    # uses an identical shape.
    seeder._peers_store.async_delay_save(seeder._serialize_peers, delay=0.0)
    for cb in seeder._shutdown_callbacks:
        await cb()

    fresh = _make_controller(config_dir=tmp_path)
    fresh._db.bus = MagicMock()
    fresh._db.devices = None
    await fresh.start()

    assert "alpha" in fresh._approved_peers
    loaded = fresh._approved_peers["alpha"]
    assert loaded.pin_sha256 == pin
    assert loaded.static_x25519_pub == pubkey
    # ``peer_ip`` survives the on-disk round-trip — the IP an
    # APPROVED row was originally paired from is what the inbox
    # / paired-senders UI shows after a restart, until a re-pair
    # refreshes it.
    assert loaded.peer_ip == "172.16.5.42"


@pytest.mark.asyncio
async def test_start_recovers_to_empty_on_corrupt_peers_file(tmp_path: Path) -> None:
    """A corrupt ``.receiver_peers.json`` doesn't crash startup; dict stays empty.

    A user (or filesystem corruption) turning the peers file into
    nonsense JSON would otherwise raise out of ``from_dict`` and
    take dashboard startup with it, locking the user out of every
    feature. Soft-recover to empty mirrors the offloader-side
    pairings store's ``_decode_pairings`` posture: every paired
    offloader has to re-pair (annoying) but the dashboard keeps
    running.
    """
    (tmp_path / ".receiver_peers.json").write_bytes(b"this is not json")

    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    controller._db.devices = None
    await controller.start()

    assert controller._approved_peers == {}


@pytest.mark.asyncio
async def test_start_loads_legacy_peers_file_without_peer_ip(tmp_path: Path) -> None:
    """A ``.receiver_peers.json`` written before ``peer_ip`` was added loads cleanly.

    The field defaults to ``""`` so legacy on-disk rows don't
    raise ``MissingField`` out of ``from_dict``; the inbox just
    shows no IP for them until a re-pair refreshes the row. Pin
    the load contract here so a future field tightening can't
    regress this without an explicit migration.
    """
    legacy = (
        b'{"peers":[{'
        b'"dashboard_id":"alpha",'
        b'"pin_sha256":"' + b"a" * 64 + b'",'
        b'"static_x25519_pub":"' + b"AA" * 32 + b'",'
        b'"label":"alpha",'
        b'"paired_at":1700000000.0'
        b"}]}"
    )
    (tmp_path / ".receiver_peers.json").write_bytes(legacy)

    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    controller._db.devices = None
    await controller.start()

    assert "alpha" in controller._approved_peers
    assert controller._approved_peers["alpha"].peer_ip == ""


@pytest.mark.asyncio
async def test_approve_peer_promotes_pending_to_approved(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    _seed_pending_peer(controller, _stored_peer(dashboard_id="alpha"))

    view = await controller.approve_peer(dashboard_id="alpha")

    assert view.peers[0].status == PeerStatus.APPROVED
    assert "alpha" not in controller._pending_peers
    # APPROVED is RAM-canonical now; the disk write happens
    # through the debounced peers Store.
    assert "alpha" in controller._approved_peers
    assert controller._approved_peers["alpha"].dashboard_id == "alpha"


@pytest.mark.asyncio
async def test_approve_peer_fires_pair_status_changed(tmp_path: Path) -> None:
    """Approval fires ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` with status=approved."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    _seed_pending_peer(controller, _stored_peer(dashboard_id="alpha"))

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
    _seed_peer(controller, _stored_peer(dashboard_id="alpha"))

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
async def test_remove_peer_drops_pending_and_fires_removed(tmp_path: Path) -> None:
    """Removing a PENDING peer fires ``status="removed"``.

    The event wakes any in-flight pair_status long-poll on the
    offloader so its listener drops the local row. Pre-refactor
    this path was silent (no event); after the in-memory-pending
    refactor PENDING and APPROVED removals fire the same event
    for a uniform wake-up signal.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    _seed_pending_peer(controller, _stored_peer(dashboard_id="alpha"))

    view = await controller.remove_peer(dashboard_id="alpha")

    assert view.peers == []
    assert "alpha" not in controller._pending_peers
    fire = controller._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED
    assert payload == {"dashboard_id": "alpha", "status": "removed"}


@pytest.mark.asyncio
async def test_remove_peer_drops_approved_and_fires_event(tmp_path: Path) -> None:
    """Removing an APPROVED peer is revocation; fires the removed event."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    _seed_peer(controller, _stored_peer(dashboard_id="alpha"))

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
    _seed_peer(controller, _stored_peer(dashboard_id="keep"))
    _seed_peer(controller, _stored_peer(dashboard_id="drop"))

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
    # Duration deliberately well above CI scheduling jitter — pre-fix
    # this was 0.1s, which was small enough that the wall-clock gap
    # between the two awaited ``set_pairing_window`` calls could
    # exceed the prune cutoff on a loaded runner. The
    # second call's ``is_pairing_window_open()`` would then prune
    # the ``tab-1`` entry, ``was_open`` would come back False, and
    # the close transition would silently not fire (count=1
    # instead of 2). Holding the duration at 1.0s leaves ~10x
    # margin against jitter; the wait below is shortened
    # accordingly so we still verify the cancelled handle never
    # fires (no real-time wait past the original deadline is
    # required — the explicit-close path schedules no replacement
    # handle, so checking ``_pairing_window_handle is None`` after
    # a tick is sufficient).
    monkeypatch.setattr(rb, "_PAIRING_WINDOW_DURATION_SECONDS", 1.0)

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

    # One scheduler tick to let any leaked-handle callback run if
    # the cancel didn't take. The handle was scheduled via
    # ``loop.call_later(remaining, ...)`` so it can only fire when
    # the loop wakes the timer queue; a single ``sleep(0)``
    # processes pending callbacks. Combined with the
    # ``_pairing_window_handle is None`` invariant (which proves
    # ``_reschedule_pairing_window_close`` cleared the slot on the
    # explicit close), this catches the regression without
    # depending on real wall-clock advance.
    await asyncio.sleep(0)

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
    await controller.set_pairing_window(open=True, client="receiver-tab")
    controller._db.bus.fire.reset_mock()
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
    # PENDING entries live in-memory; APPROVED dict is empty.
    assert controller._approved_peers == {}
    pending = controller._pending_peers["alpha"]
    assert pending.pin_sha256 == pin
    assert pending.static_x25519_pub == pubkey
    assert pending.label == "alpha"


@pytest.mark.asyncio
async def test_record_pair_request_fires_event(tmp_path: Path) -> None:
    """Creating a PENDING row fires REMOTE_BUILD_PAIR_REQUEST_RECEIVED."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await controller.set_pairing_window(open=True, client="receiver-tab")
    controller._db.bus.fire.reset_mock()
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
    # The event carries the same ``paired_at`` the StoredPeer
    # got — the controller emits a single timestamp into both so
    # a frontend rebuilding the inbox row from the event matches
    # the snapshot. Verify by reading the stored value back.
    stored = controller._pending_peers["alpha"]
    assert payload == {
        "dashboard_id": "alpha",
        "pin_sha256": pin,
        "label": "alpha",
        "peer_ip": "192.168.1.10",
        "paired_at": stored.paired_at,
    }
    # ``peer_ip`` is persisted on the StoredPeer (rather than
    # carried only on the live event) so a snapshot-loaded
    # PENDING row still surfaces the IP for the operator's
    # clone-risk sanity-check.
    assert stored.peer_ip == "192.168.1.10"


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
    )
    _seed_pending_peer(controller, initial)
    await controller.set_pairing_window(open=True, client="receiver-tab")
    controller._db.bus.fire.reset_mock()
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
    # PENDING refresh updates the in-memory dict, not disk.
    refreshed = controller._pending_peers["alpha"]
    assert refreshed.pin_sha256 == new_pin
    assert refreshed.static_x25519_pub == new_pubkey
    assert refreshed.label == "renamed"
    assert refreshed.paired_at > 1.0
    # ``peer_ip`` refreshes too — the offloader could be on a
    # different interface / DHCP-renewed since the original
    # pair attempt, and the inbox should show the source the
    # current handshake came from.
    assert refreshed.peer_ip == "10.0.0.1"
    # APPROVED dict stays empty — refresh is PENDING-only.
    assert controller._approved_peers == {}


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
    )
    _seed_peer(controller, approved)

    response = await controller.record_pair_request(
        dashboard_id="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label="renamed-but-ignored",
        peer_ip="10.0.0.1",
    )

    assert response == "approved"
    [peer] = controller._approved_peers.values()
    assert peer.pin_sha256 == pin
    assert peer.label == "alpha"
    assert peer.paired_at == 1.0
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_record_pair_request_unknown_dashboard_id_closed_window_returns_no_pairing_window(
    tmp_path: Path,
) -> None:
    """A new offloader hitting pair_request while window is closed returns NO_PAIRING_WINDOW.

    The pairing window only gates branches that would create or
    refresh a PENDING row (new authorization). With no row for
    the offloader yet and the window closed, the result is
    NO_PAIRING_WINDOW — admin needs to open the screen for new
    pair-requests to even be accepted.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\x33" * 32
    pin = hashlib.sha256(pubkey).hexdigest()

    response = await controller.record_pair_request(
        dashboard_id="newcomer",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label="newcomer",
        peer_ip="10.0.0.1",
    )

    assert response is IntentResponse.NO_PAIRING_WINDOW
    # No row was created — the gate fired before the insert.
    assert controller._approved_peers == {}
    assert controller._pending_peers == {}
    # No event fired either — the pair_request_received event is
    # for surfacing rows in the inbox, and no row was created.
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_record_pair_request_already_approved_bypasses_closed_window(
    tmp_path: Path,
) -> None:
    """An already-approved peer re-pairing with matching pin bypasses the window gate.

    The window only narrows when *new authorization* is being
    requested. A pair_request from a peer whose pubkey matches
    an APPROVED row is just re-establishing existing trust —
    admin doesn't need to be on the Pairing requests screen for
    that to work. Otherwise an offloader's pair-request retry
    after a network blip would surface NO_PAIRING_WINDOW just
    because the receiver-side admin closed the screen between
    pairings, forcing the user to re-engage with no security
    benefit.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\x44" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    approved = _stored_peer(
        dashboard_id="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label="alpha",
        paired_at=1.0,
    )
    _seed_peer(controller, approved)
    # Window stays CLOSED — no set_pairing_window call.

    response = await controller.record_pair_request(
        dashboard_id="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label="alpha",
        peer_ip="10.0.0.1",
    )

    assert response is IntentResponse.APPROVED


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
    )
    _seed_peer(controller, approved)

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
    [peer] = controller._approved_peers.values()
    # Original row untouched.
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
    _seed_peer(
        controller,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
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
    _seed_pending_peer(
        controller,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
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
    _seed_peer(
        controller,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=stored_pin,
            static_x25519_pub=stored_pubkey,
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
    _seed_peer(
        controller,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
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


@pytest.mark.asyncio
async def test_lookup_peer_for_status_long_polls_until_approve_fires(
    tmp_path: Path,
) -> None:
    """Long-poll wakes promptly when ``approve_peer`` flips the row mid-wait.

    Mirrors the production wire flow: an offloader's ``intent="pair_status"``
    arrives while its row is still PENDING; the receiver's admin clicks
    Accept on a second WS, which fires ``REMOTE_BUILD_PAIR_STATUS_CHANGED``
    on the bus; the long-poll wakes, re-snapshots the now-APPROVED row,
    returns ``APPROVED``. Uses a real :class:`EventBus` so the listener
    machinery actually runs (the MagicMock fixture used elsewhere doesn't
    deliver events).
    """
    controller = _make_controller(config_dir=tmp_path, real_bus=True)
    pubkey = b"\x55" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    # Open the window first so the dict-clear-on-close path doesn't
    # immediately wipe what we seed below.
    await controller.set_pairing_window(open=True, client="receiver-tab")
    _seed_pending_peer(
        controller,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
        ),
    )

    async def _flip_after_short_delay() -> None:
        # Yield enough loop ticks that ``lookup_peer_for_status`` has
        # time to register its bus listener and park on the wait.
        await asyncio.sleep(0.05)
        await controller.approve_peer(dashboard_id="alpha")

    flip_task = asyncio.create_task(_flip_after_short_delay())
    try:
        response = await controller.lookup_peer_for_status(dashboard_id="alpha", pin_sha256=pin)
    finally:
        await flip_task
        await controller.stop()

    assert response is IntentResponse.APPROVED


@pytest.mark.asyncio
async def test_lookup_peer_for_status_long_poll_ignores_other_dashboard_ids(
    tmp_path: Path,
) -> None:
    """Bus events for unrelated ``dashboard_id`` don't wake the long-poll.

    Two pending rows; firing approve_peer for bravo must not unpark
    alpha's waiter. Closing the pairing window then deterministically
    ends alpha's long-poll: window-close fires
    ``pair_status_changed`` for each cleared dict entry (including
    alpha), the listener wakes, re-snapshots, finds alpha gone, and
    returns ``REJECTED``.
    """
    controller = _make_controller(config_dir=tmp_path, real_bus=True)
    pubkey = b"\x66" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    await controller.set_pairing_window(open=True, client="receiver-tab")
    _seed_pending_peer(
        controller,
        _stored_peer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
        ),
    )
    _seed_pending_peer(
        controller,
        _stored_peer(
            dashboard_id="bravo",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
        ),
    )

    async def _approve_bravo_then_close_window() -> None:
        # Yield long enough for alpha's long-poll to park.
        await asyncio.sleep(0.02)
        await controller.approve_peer(dashboard_id="bravo")
        # Bravo's flip event must NOT have unparked alpha. Then
        # close the window to deterministically end alpha's wait
        # (else the test parks indefinitely — no timeout exists).
        await asyncio.sleep(0.02)
        await controller.set_pairing_window(open=False, client="receiver-tab")

    flip_task = asyncio.create_task(_approve_bravo_then_close_window())
    try:
        response = await controller.lookup_peer_for_status(dashboard_id="alpha", pin_sha256=pin)
    finally:
        await flip_task
        await controller.stop()

    # Window-close cleared alpha from the pending dict + fired the
    # removal event; alpha's re-snapshot misses → REJECTED. NOT
    # APPROVED (which would mean bravo's flip woke alpha's waiter
    # incorrectly).
    assert response is IntentResponse.REJECTED


# ---------------------------------------------------------------------------
# Offloader-side pair-flow helpers (phase 4a-o part 3)
# ---------------------------------------------------------------------------


def _valid_stored_pairing(
    *,
    receiver_hostname: str = "build.local",
    receiver_port: int = 6055,
    label: str = "desktop",
    paired_at: float = 1.0,
    status: PeerStatus = PeerStatus.APPROVED,
) -> StoredPairing:
    """Build a passing :class:`StoredPairing` so tests don't repeat the boilerplate.

    Defaults to APPROVED — that's the on-disk shape. PENDING is
    only ever in-RAM (the controller filters PENDING out at
    serialise time), so tests that want a PENDING row in the
    controller's dict opt in explicitly.
    """
    return StoredPairing(
        receiver_hostname=receiver_hostname,
        receiver_port=receiver_port,
        pin_sha256="a" * 64,
        static_x25519_pub=b"\x01" * 32,
        label=label,
        paired_at=paired_at,
        status=status,
    )


# --- _validate_pin_sha256 ---


def test_validate_pin_sha256_accepts_canonical() -> None:
    assert _validate_pin_sha256("a" * 64) == "a" * 64
    assert _validate_pin_sha256("0123456789abcdef" * 4) == "0123456789abcdef" * 4


def test_validate_pin_sha256_strips_whitespace() -> None:
    assert _validate_pin_sha256("  " + "a" * 64 + "  ") == "a" * 64


@pytest.mark.parametrize(
    "bad",
    [
        "a" * 63,  # too short
        "a" * 65,  # too long
        "A" * 64,  # uppercase
        "z" * 64,  # outside hex alphabet
    ],
)
def test_validate_pin_sha256_rejects_invalid_shapes(bad: str) -> None:
    with pytest.raises(CommandError) as exc:
        _validate_pin_sha256(bad)
    assert exc.value.code == ErrorCode.INVALID_ARGS


def test_validate_pin_sha256_rejects_non_string() -> None:
    with pytest.raises(CommandError) as exc:
        _validate_pin_sha256(123)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_ARGS


# --- _validate_pair_label ---


def test_validate_pair_label_strips_and_returns() -> None:
    assert _validate_pair_label("  Kitchen  ", field=_PairLabelField.RECEIVER_LABEL) == "Kitchen"


def test_validate_pair_label_accepts_empty() -> None:
    """Empty label is fine; user may not have named the receiver."""
    assert _validate_pair_label("", field=_PairLabelField.RECEIVER_LABEL) == ""


def test_validate_pair_label_rejects_oversize() -> None:
    with pytest.raises(CommandError) as exc:
        _validate_pair_label("x" * 129, field=_PairLabelField.OFFLOADER_LABEL)
    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert "offloader_label" in str(exc.value)


def test_validate_pair_label_rejects_non_string() -> None:
    with pytest.raises(CommandError) as exc:
        _validate_pair_label(42, field=_PairLabelField.RECEIVER_LABEL)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert "receiver_label" in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("evil\x1b[31mboo", id="ansi_escape"),
        pytest.param("two\nlines", id="newline"),
        pytest.param("car\rriage", id="carriage_return"),
        pytest.param("null\x00byte", id="null_byte"),
        pytest.param("zero\u200bwidth", id="zero_width"),
        pytest.param("bidi\u202eflip", id="bidi_override"),
    ],
)
def test_validate_pair_label_rejects_control_characters(payload: str) -> None:
    """Control / bidi / zero-width chars are rejected (defense-in-depth).

    The ``offloader_label`` transits to the receiver-side admin
    inbox; a malicious offloader must not be able to inject ANSI
    escapes, newlines, null bytes, or bidi-override Unicode that
    could fake a label's identity in a terminal-rendered admin
    tool.
    """
    with pytest.raises(CommandError) as exc:
        _validate_pair_label(payload, field=_PairLabelField.OFFLOADER_LABEL)
    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert "printable" in str(exc.value)


def test_validate_pair_label_accepts_non_ascii_printables() -> None:
    """Non-ASCII printables (CJK, accented Latin, emoji) round-trip cleanly."""
    assert _validate_pair_label("キッチン", field=_PairLabelField.RECEIVER_LABEL) == "キッチン"
    assert _validate_pair_label("café 🚀", field=_PairLabelField.RECEIVER_LABEL) == "café 🚀"


# --- _intent_response_to_command_error ---


def test_intent_response_to_command_error_pending_returns_none() -> None:
    """Success values aren't translated; the caller branches on them for persistence."""
    assert _intent_response_to_command_error(IntentResponse.PENDING) is None
    assert _intent_response_to_command_error(IntentResponse.APPROVED) is None
    assert _intent_response_to_command_error(IntentResponse.OK) is None


def test_intent_response_to_command_error_no_pairing_window() -> None:
    err = _intent_response_to_command_error(IntentResponse.NO_PAIRING_WINDOW)
    assert err is not None
    assert err.code == ErrorCode.NO_PAIRING_WINDOW


def test_intent_response_to_command_error_rejected() -> None:
    err = _intent_response_to_command_error(IntentResponse.REJECTED)
    assert err is not None
    assert err.code == ErrorCode.PRECONDITION_FAILED


# --- _enforce_pin_match ---


def test_enforce_pin_match_passes_on_match() -> None:
    """No exception raised when expected and observed pins agree."""
    _enforce_pin_match(expected="a" * 64, observed="a" * 64)


def test_enforce_pin_match_raises_precondition_failed_on_drift() -> None:
    """A pubkey-hash drift between preview and request → PRECONDITION_FAILED.

    The error message carries both pins in full (no
    truncation) so the user can do a full side-by-side OOB
    comparison; an attacker who collides a 16-char prefix
    can't slip the mismatch past a quick visual scan.
    """
    with pytest.raises(CommandError) as exc:
        _enforce_pin_match(expected="a" * 64, observed="b" * 64)
    assert exc.value.code == ErrorCode.PRECONDITION_FAILED
    # Full digest is shown for both expected and observed.
    assert "expected " + "a" * 64 in str(exc.value)
    assert "got " + "b" * 64 in str(exc.value)


# --- _pairing_summary ---


def test_pairing_summary_drops_static_pubkey() -> None:
    """Wire view drops ``static_x25519_pub`` so the raw pubkey stays server-side."""
    pairing = _valid_stored_pairing(status=PeerStatus.PENDING)
    summary = _pairing_summary(pairing, connected=False)
    assert isinstance(summary, PairingSummary)
    assert summary.receiver_hostname == "build.local"
    assert summary.pin_sha256 == "a" * 64
    assert summary.label == "desktop"
    assert summary.status is PeerStatus.PENDING
    assert summary.connected is False
    # PairingSummary doesn't carry the raw bytes.
    assert not hasattr(summary, "static_x25519_pub")


# --- _decode_pairings / _encode_pairings ---


def test_encode_decode_pairings_round_trip() -> None:
    """Encoded bytes round-trip back through the decoder unchanged."""
    settings = OffloaderRemoteBuildSettings(pairings=[_valid_stored_pairing()])
    payload = _encode_pairings(settings)
    decoded = _decode_pairings(payload)
    assert decoded == settings


def test_decode_pairings_recovers_to_empty_on_garbage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt or unparsable file resets to empty + logs the exception.

    The dashboard's policy on a corrupt offloader pairings file is
    soft-recovery: every offloader has to re-pair (annoying but
    not fatal), versus crashing the dashboard at startup which
    would lock the user out entirely. The recovery is observable
    via the logged exception so an operator can see *why* their
    pairings vanished.
    """
    with caplog.at_level("ERROR"):
        result = _decode_pairings(b"this is not json {{{")
    assert result == OffloaderRemoteBuildSettings()
    assert any("Corrupt offloader pairings file" in r.message for r in caplog.records), (
        "expected corruption-recovery log line"
    )


def test_decode_pairings_recovers_to_empty_on_schema_drift(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JSON parses but ``OffloaderRemoteBuildSettings.from_dict`` rejects → empty.

    Pins the second branch of the recovery: valid JSON but a
    payload mashumaro can't coerce (e.g. a list at the top level
    where a dict is expected, or a future-shape sidecar a
    downgraded dashboard read).
    """
    with caplog.at_level("ERROR"):
        result = _decode_pairings(b'["unexpected", "list", "shape"]')
    assert result == OffloaderRemoteBuildSettings()
    assert any("Corrupt offloader pairings file" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Phase 5b: queue_status receiver-side broadcast + offloader-side cache
# ---------------------------------------------------------------------------


def _make_session_stub(dashboard_id: str) -> AsyncMock:
    """Build a ``PeerLinkSession`` stand-in with an ``AsyncMock`` send.

    Tests of the broadcast path care about (a) which sessions
    received the frame and (b) what payload landed there. A
    full session would require a Noise transcript; here a stub
    that records ``send_app_frame`` calls is enough.
    """
    session = MagicMock()
    session.dashboard_id = dashboard_id
    session.send_app_frame = AsyncMock(return_value=True)
    return session


@pytest.mark.asyncio
async def test_on_firmware_queue_transition_broadcasts_to_every_session(
    tmp_path: Path,
) -> None:
    """A ``JOB_STARTED`` tick broadcasts a fresh snapshot to all paired offloaders."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.firmware = MagicMock()
    controller._db.firmware.queue_status_snapshot.return_value = (False, True, 2)
    # ``create_background_task`` on the real ``DeviceBuilder``
    # schedules onto the running loop; the MagicMock-backed
    # parent here doesn't have a loop, so route through the
    # actual event loop directly.
    controller._db.create_background_task = asyncio.create_task

    alpha = _make_session_stub("alpha")
    beta = _make_session_stub("beta")
    controller._peer_link_sessions["alpha"] = alpha
    controller._peer_link_sessions["beta"] = beta

    controller._on_firmware_queue_transition(
        MagicMock(event_type=EventType.JOB_STARTED, data={"job_id": "j1"})
    )
    # Background task scheduled; yield until both sessions saw the send.
    for _ in range(50):
        if alpha.send_app_frame.await_count and beta.send_app_frame.await_count:
            break
        await asyncio.sleep(0)

    expected_payload = {
        "type": "queue_status",
        "idle": False,
        "running": True,
        "queue_depth": 2,
    }
    alpha.send_app_frame.assert_awaited_once_with(expected_payload)
    beta.send_app_frame.assert_awaited_once_with(expected_payload)


def test_on_firmware_queue_transition_skips_when_no_sessions(tmp_path: Path) -> None:
    """No paired offloaders → no background task scheduled."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.firmware = MagicMock()
    controller._db.firmware.queue_status_snapshot.return_value = (True, False, 0)
    controller._db.create_background_task = MagicMock()

    controller._on_firmware_queue_transition(
        MagicMock(event_type=EventType.JOB_COMPLETED, data={"job_id": "j1"})
    )

    controller._db.create_background_task.assert_not_called()


def test_on_firmware_queue_transition_skips_when_firmware_missing(
    tmp_path: Path,
) -> None:
    """Bus tick when ``DeviceBuilder.firmware`` is ``None`` short-circuits.

    ``RemoteBuildController.start`` registers the listener
    before :class:`DeviceBuilder` finishes wiring all
    controllers, but the ``firmware`` attribute is set first
    in startup; still, a partial-startup race where the bus
    fires before ``firmware`` lands shouldn't crash. Confirm
    the early-exit path doesn't hit the snapshot helper.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.firmware = None
    controller._db.create_background_task = MagicMock()

    controller._on_firmware_queue_transition(
        MagicMock(event_type=EventType.JOB_QUEUED, data={"job_id": "j1"})
    )

    controller._db.create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_queue_status_continues_past_failed_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A session whose ``send_app_frame`` raises doesn't block sibling sessions.

    Phase-5b's broadcast is best-effort per session: a closed
    session (race with concurrent terminate), a transport
    error, or any other unexpected raise on one session must
    not cancel the broadcast for the others. The per-session
    ``try/except`` in :meth:`_broadcast_queue_status` swallows
    the raise (logged) and moves on. Without this contract a
    single flaky peer would starve every paired offloader of
    queue-status updates.
    """
    controller = _make_controller(config_dir=tmp_path)

    alpha = _make_session_stub("alpha")
    alpha.send_app_frame = AsyncMock(side_effect=RuntimeError("boom"))
    beta = _make_session_stub("beta")
    controller._peer_link_sessions["alpha"] = alpha
    controller._peer_link_sessions["beta"] = beta

    with caplog.at_level("ERROR"):
        await controller._broadcast_queue_status(idle=False, running=True, queue_depth=1)
    alpha.send_app_frame.assert_awaited_once()
    # Sibling session received the snapshot despite alpha raising.
    beta.send_app_frame.assert_awaited_once_with(
        {"type": "queue_status", "idle": False, "running": True, "queue_depth": 1}
    )
    # Per-session failure landed in the log so a flaky peer is
    # visible in production rather than silently dropped.
    assert any(
        "queue_status broadcast to session alpha raised" in record.message
        for record in caplog.records
    )


def test_on_offloader_pair_pin_mismatch_caches_alert(tmp_path: Path) -> None:
    """``OFFLOADER_PAIR_PIN_MISMATCH`` listener caches the alert in ``_offloader_alerts``.

    The peer-link path's pin-check (4a-o part 5) fires
    ``OFFLOADER_PAIR_PIN_MISMATCH`` from the
    :class:`PeerLinkClient` when ``session.remote_static_pub``
    drifts from the pinned pubkey. The controller listens and
    populates ``_offloader_alerts`` with a snapshot row so the
    ``initial_state.offloader_alerts`` push picks it up for
    late-subscribing tabs.
    """
    controller = _make_controller(config_dir=tmp_path)
    payload = {
        "receiver_hostname": "host.local",
        "receiver_port": 6055,
        "receiver_label": "my-laptop",
        "pin_sha256": "a" * 64,
        "expected_pin": "a" * 64,
        "observed_pin": "b" * 64,
    }
    controller._on_offloader_pair_pin_mismatch(MagicMock(data=payload))

    cached = controller._offloader_alerts["a" * 64]
    assert cached["kind"] == "pin_mismatch"
    assert cached["receiver_hostname"] == "host.local"
    assert cached["receiver_port"] == 6055
    assert cached["pin_sha256"] == "a" * 64
    assert cached["receiver_label"] == "my-laptop"
    assert cached["expected_pin"] == "a" * 64
    assert cached["observed_pin"] == "b" * 64
    assert "fired_at" in cached  # set by the listener at fire-time


def test_on_offloader_queue_status_changed_caches_snapshot(tmp_path: Path) -> None:
    """Inbound bus event lands a per-peer snapshot in the cache."""
    controller = _make_controller(config_dir=tmp_path)
    payload = {
        "receiver_hostname": "192.168.1.10",
        "receiver_port": 6055,
        "pin_sha256": "a" * 64,
        "idle": False,
        "running": True,
        "queue_depth": 3,
    }
    controller._on_offloader_queue_status_changed(MagicMock(data=payload))

    cached = controller._peer_queue_status["a" * 64]
    assert cached["receiver_hostname"] == "192.168.1.10"
    assert cached["receiver_port"] == 6055
    assert cached["pin_sha256"] == "a" * 64
    assert cached["idle"] is False
    assert cached["running"] is True
    assert cached["queue_depth"] == 3


def test_on_offloader_queue_status_changed_overwrites_prior(tmp_path: Path) -> None:
    """A second event for the same key replaces the prior snapshot."""
    controller = _make_controller(config_dir=tmp_path)
    pin = "a" * 64
    first = {
        "receiver_hostname": "host",
        "receiver_port": 6055,
        "pin_sha256": pin,
        "idle": True,
        "running": False,
        "queue_depth": 0,
    }
    second = {
        "receiver_hostname": "host",
        "receiver_port": 6055,
        "pin_sha256": pin,
        "idle": False,
        "running": True,
        "queue_depth": 5,
    }
    controller._on_offloader_queue_status_changed(MagicMock(data=first))
    controller._on_offloader_queue_status_changed(MagicMock(data=second))

    cached = controller._peer_queue_status[pin]
    assert cached["queue_depth"] == 5
    assert cached["running"] is True
    assert len(controller._peer_queue_status) == 1


def test_peer_queue_status_snapshot_returns_list(tmp_path: Path) -> None:
    """``peer_queue_status_snapshot`` returns a list of cached entries."""
    controller = _make_controller(config_dir=tmp_path)
    controller._peer_queue_status["a" * 64] = {
        "receiver_hostname": "a",
        "receiver_port": 6055,
        "pin_sha256": "a" * 64,
        "idle": True,
        "running": False,
        "queue_depth": 0,
    }
    controller._peer_queue_status["b" * 64] = {
        "receiver_hostname": "b",
        "receiver_port": 6055,
        "pin_sha256": "b" * 64,
        "idle": False,
        "running": True,
        "queue_depth": 2,
    }
    snapshot = controller.peer_queue_status_snapshot()
    assert len(snapshot) == 2
    hostnames = {entry["receiver_hostname"] for entry in snapshot}
    assert hostnames == {"a", "b"}


def test_get_submit_job_receiver_raises_before_start(tmp_path: Path) -> None:
    """Accessing ``get_submit_job_receiver`` before ``start()`` raises ``RuntimeError``.

    Pins the bring-up ordering invariant: the wire dispatch in
    :func:`controllers.remote_build.peer_link._receive_loop`
    reaches the receiver via this accessor, and the peer-link
    listener only binds after :meth:`start` has installed it.
    The explicit failure surfaces a future bring-up regression
    instead of silently no-op'ing the dispatch.
    """
    controller = _make_controller(config_dir=tmp_path)
    with pytest.raises(RuntimeError, match=r"before RemoteBuildController\.start"):
        controller.get_submit_job_receiver()
