"""
Tests for the ``--remote-build-only`` headless service mode.

Covers the first-pair auto-approve branch in ``pair_flow``, the
bootstrap orchestration + blocking runner in ``_remote_build_only``,
the CLI / settings plumbing, the ``maybe_start`` force-enable
bypass, and the banner's Matrix-SAS fingerprint rendering.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.web import GracefulExit

from esphome_device_builder import __main__ as main_module
from esphome_device_builder import _remote_build_only as rbo
from esphome_device_builder.controllers.config import (
    DashboardSettings,
    remote_build_settings_transaction,
)
from esphome_device_builder.controllers.config.settings import normalize_pairing_sources
from esphome_device_builder.controllers.remote_build import (
    pairing_window as rb_pairing_window,
)
from esphome_device_builder.controllers.remote_build._storage_codecs import (
    RECEIVER_PEERS_FILE,
)
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.helpers.pin_emoji import pin_emoji, pin_emoji_names
from esphome_device_builder.models import EventType, StoredPeer

from .conftest import MakeSettingsFactory, make_remote_build_controller
from .conftest import RemoteBuildTestHandles as RemoteBuildController

_RBO_LOGGER = "esphome_device_builder._remote_build_only"


def _pubkey_and_pin(seed: bytes) -> tuple[bytes, str]:
    pubkey = seed * 32
    return pubkey, hashlib.sha256(pubkey).hexdigest()


async def _send_pair_request(
    controller: RemoteBuildController,
    *,
    dashboard_id: str = "main-builder",
    seed: bytes = b"\xaa",
    label: str = "Main builder",
    peer_ip: str = "192.168.1.10",
) -> Any:
    pubkey, pin = _pubkey_and_pin(seed)
    return await controller.receiver.record_pair_request(
        dashboard_id=dashboard_id,
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label=label,
        peer_ip=peer_ip,
    )


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


class _FakeDB:
    """Minimal ``DeviceBuilder`` stand-in for driving the headless runner."""

    def __init__(
        self,
        handles: RemoteBuildController,
        *,
        listener_bound: bool = True,
        receiver_present: bool = True,
        seed_approved_peer: bool = False,
        raise_graceful_after_start: bool = False,
        stop_error: Exception | None = None,
    ) -> None:
        self._handles = handles
        self.bus = handles.receiver._db.bus
        self.remote_build_receiver = handles.receiver if receiver_present else None
        self.is_remote_build_listener_bound = listener_bound
        self.settings = handles.receiver._db.settings
        self.settings.remote_build_port = 6055
        self.peer_link_identity_store = handles.receiver._db.peer_link_identity_store
        self._seed_approved_peer = seed_approved_peer
        self._raise_graceful_after_start = raise_graceful_after_start
        self._stop_error = stop_error
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        if self._seed_approved_peer:
            pubkey, pin = _pubkey_and_pin(b"\xaa")
            self._handles.receiver.state.approved_peers["main-builder"] = StoredPeer(
                dashboard_id="main-builder",
                pin_sha256=pin,
                static_x25519_pub=pubkey,
                label="Main builder",
                paired_at=0.0,
                peer_ip="192.168.1.10",
            )
        if self._raise_graceful_after_start:
            asyncio.get_running_loop().call_later(0.05, self._raise_graceful)

    async def stop(self) -> None:
        self.stopped = True
        if self._stop_error is not None:
            raise self._stop_error

    @staticmethod
    def _raise_graceful() -> None:
        # Mirrors production's SIGTERM path (a scheduled callback that
        # raises GracefulExit). GracefulExit subclasses SystemExit, so
        # asyncio re-raises it out of run_until_complete rather than
        # swallowing it into the loop exception handler — the runner's
        # ``except`` catches it deterministically, no hang.
        raise GracefulExit


def _make_fake_db(config_dir: Path, **kwargs: Any) -> _FakeDB:
    handles = make_remote_build_controller(config_dir=config_dir, bus=EventBus())
    return _FakeDB(handles, **kwargs)


# ---------------------------------------------------------------------------
# pair_flow auto-approve branch
# ---------------------------------------------------------------------------


async def test_auto_approve_first_pair_approves_and_persists(tmp_path: Path) -> None:
    """Armed + open window + zero APPROVED rows → direct APPROVED, flushed to disk."""
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    await controller.receiver.set_pairing_window(open=True, client="cli")
    controller.receiver.state.auto_approve_first_pair = True
    controller.offloader._db.bus.fire.reset_mock()

    response = await _send_pair_request(controller)

    assert response.response == "approved"
    assert controller.receiver.state.pending_peers == {}
    peer = controller.receiver.state.approved_peers["main-builder"]
    assert peer.label == "Main builder"
    assert peer.peer_ip == "192.168.1.10"
    assert not controller.receiver.state.auto_approve_first_pair
    # Flushed before the wire response, not debounced.
    assert (tmp_path / RECEIVER_PEERS_FILE).is_file()
    # Exactly one event: the approved status flip. No
    # REQUEST_RECEIVED — there is no inbox row to surface.
    fire = controller.offloader._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED
    assert payload == {"dashboard_id": "main-builder", "status": "approved"}


async def test_auto_approve_skipped_when_a_peer_is_already_approved(tmp_path: Path) -> None:
    """The guard refuses a second trust grant even while the flag is armed."""
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    await controller.receiver.set_pairing_window(open=True, client="cli")
    await _send_pair_request(controller, dashboard_id="first", seed=b"\xaa")
    controller.receiver.state.pending_peers.clear()
    controller.receiver.state.approved_peers["first"] = StoredPeer(
        dashboard_id="first",
        pin_sha256=_pubkey_and_pin(b"\xaa")[1],
        static_x25519_pub=b"\xaa" * 32,
        label="first",
        paired_at=0.0,
        peer_ip="192.168.1.10",
    )
    controller.receiver.state.auto_approve_first_pair = True

    response = await _send_pair_request(controller, dashboard_id="second", seed=b"\xbb")

    assert response.response == "pending"
    assert "second" in controller.receiver.state.pending_peers
    assert "second" not in controller.receiver.state.approved_peers
    # The flag stays armed — it never fired.
    assert controller.receiver.state.auto_approve_first_pair


async def test_auto_approve_is_one_shot(tmp_path: Path) -> None:
    """A second request inside the same window lands PENDING, not APPROVED."""
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    await controller.receiver.set_pairing_window(open=True, client="cli")
    controller.receiver.state.auto_approve_first_pair = True

    first = await _send_pair_request(controller, dashboard_id="first", seed=b"\xaa")
    second = await _send_pair_request(controller, dashboard_id="second", seed=b"\xbb")

    assert first.response == "approved"
    assert second.response == "pending"
    assert list(controller.receiver.state.approved_peers) == ["first"]
    assert list(controller.receiver.state.pending_peers) == ["second"]


async def test_auto_approve_honours_source_allowlist(tmp_path: Path) -> None:
    """A request from a non-allowlisted source is refused without disarming."""
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    controller.receiver._db.settings.allow_pairing_sources = ["192.168.1.50"]
    await controller.receiver.set_pairing_window(open=True, client="cli")
    controller.receiver.state.auto_approve_first_pair = True

    # Wrong source: refused as a closed window, nothing approved, still armed.
    stranger = await _send_pair_request(
        controller, dashboard_id="stranger", seed=b"\xbb", peer_ip="192.168.1.99"
    )
    assert stranger.response == "no_pairing_window"
    assert controller.receiver.state.approved_peers == {}
    assert controller.receiver.state.pending_peers == {}
    assert controller.receiver.state.auto_approve_first_pair

    # The intended builder still pairs.
    wanted = await _send_pair_request(
        controller, dashboard_id="main", seed=b"\xaa", peer_ip="192.168.1.50"
    )
    assert wanted.response == "approved"
    assert list(controller.receiver.state.approved_peers) == ["main"]
    assert not controller.receiver.state.auto_approve_first_pair


async def test_auto_approve_source_allowlist_normalises_ipv6(tmp_path: Path) -> None:
    """A differently-spelled but equal IPv6 source still matches."""
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    controller.receiver._db.settings.allow_pairing_sources = ["2001:db8::1"]
    await controller.receiver.set_pairing_window(open=True, client="cli")
    controller.receiver.state.auto_approve_first_pair = True

    response = await _send_pair_request(
        controller, dashboard_id="main", peer_ip="2001:0db8:0000:0000:0000:0000:0000:0001"
    )
    assert response.response == "approved"


async def test_auto_approve_source_allowlist_rejects_unparseable_peer_ip(tmp_path: Path) -> None:
    """A peer_ip that can't be parsed as an IP never matches an allowlist."""
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    controller.receiver._db.settings.allow_pairing_sources = ["192.168.1.50"]
    await controller.receiver.set_pairing_window(open=True, client="cli")
    controller.receiver.state.auto_approve_first_pair = True

    response = await _send_pair_request(controller, dashboard_id="main", peer_ip="")
    assert response.response == "no_pairing_window"
    assert controller.receiver.state.approved_peers == {}


# ---------------------------------------------------------------------------
# _bootstrap_first_pair
# ---------------------------------------------------------------------------


async def test_bootstrap_first_pair_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A pair request during the window auto-approves; the window closes behind it."""
    db = _make_fake_db(tmp_path)
    receiver = db.remote_build_receiver
    assert receiver is not None

    with caplog.at_level("INFO", logger=_RBO_LOGGER):
        bootstrap = asyncio.create_task(rbo._bootstrap_first_pair(db, receiver))
        await _wait_until(receiver.is_pairing_window_open)
        assert receiver.state.auto_approve_first_pair

        response = await _send_pair_request(RemoteBuildController(MagicMock(), receiver))
        assert response.response == "approved"
        assert await asyncio.wait_for(bootstrap, timeout=2.0) is True

    assert not receiver.is_pairing_window_open()
    assert not receiver.state.auto_approve_first_pair
    identity = await db.peer_link_identity_store.async_load()
    banner = next(
        r.getMessage() for r in caplog.records if "REMOTE BUILD PAIRING" in r.getMessage()
    )
    assert pin_emoji(identity.pin_sha256) in banner
    assert pin_emoji_names(identity.pin_sha256) in banner
    assert identity.pin_sha256_formatted in banner
    # No allowlist = the operator chose --allow-any-pairing-source; the
    # banner names that explicit any-source posture.
    assert "--allow-any-pairing-source" in banner
    assert "ANY source" in banner
    assert any("Paired with 'Main builder'" in r.getMessage() for r in caplog.records)


async def test_bootstrap_first_pair_with_source_allowlist(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The banner names the allowlisted source; only that source auto-approves."""
    db = _make_fake_db(tmp_path)
    db.settings.allow_pairing_sources = ["192.168.1.10"]
    receiver = db.remote_build_receiver
    assert receiver is not None

    with caplog.at_level("INFO", logger=_RBO_LOGGER):
        bootstrap = asyncio.create_task(rbo._bootstrap_first_pair(db, receiver))
        await _wait_until(receiver.is_pairing_window_open)

        # A stranger is refused; the window stays armed.
        refused = await _send_pair_request(
            RemoteBuildController(MagicMock(), receiver),
            dashboard_id="stranger",
            peer_ip="10.0.0.9",
        )
        assert refused.response == "no_pairing_window"
        assert not bootstrap.done()

        # The allowlisted builder pairs.
        ok = await _send_pair_request(RemoteBuildController(MagicMock(), receiver))
        assert ok.response == "approved"
        assert await asyncio.wait_for(bootstrap, timeout=2.0) is True

    banner = next(
        r.getMessage() for r in caplog.records if "REMOTE BUILD PAIRING" in r.getMessage()
    )
    assert "Only auto-approving a request from: 192.168.1.10" in banner


async def test_bootstrap_first_pair_window_lapse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unpaired window lapse returns False and disarms the flag."""
    monkeypatch.setattr(rb_pairing_window, "PAIRING_WINDOW_DURATION_SECONDS", 0.05)
    db = _make_fake_db(tmp_path)
    receiver = db.remote_build_receiver
    assert receiver is not None

    with caplog.at_level("ERROR", logger=_RBO_LOGGER):
        assert await rbo._bootstrap_first_pair(db, receiver) is False

    assert not receiver.state.auto_approve_first_pair
    assert receiver.state.approved_peers == {}
    assert any("No pairing request arrived" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _serve
# ---------------------------------------------------------------------------


async def test_serve_returns_1_when_receiver_missing(tmp_path: Path) -> None:
    """No receiver controller → nothing to serve, exit code 1."""
    db = _make_fake_db(tmp_path, receiver_present=False)
    assert await rbo._serve(db) == 1  # type: ignore[arg-type]
    assert db.started


async def test_serve_parks_after_bootstrap_pair(tmp_path: Path) -> None:
    """After a successful first pair the serve task keeps running until cancelled."""
    db = _make_fake_db(tmp_path)
    receiver = db.remote_build_receiver
    assert receiver is not None
    serve = asyncio.create_task(rbo._serve(db))  # type: ignore[arg-type]

    await _wait_until(receiver.is_pairing_window_open)
    await _send_pair_request(RemoteBuildController(MagicMock(), receiver))
    await _wait_until(lambda: not receiver.is_pairing_window_open())
    await asyncio.sleep(0.05)

    assert not serve.done()
    serve.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serve


async def test_serve_returns_1_when_bootstrap_lapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lapsed first-pair window makes serve report the not-serving exit code."""
    monkeypatch.setattr(rb_pairing_window, "PAIRING_WINDOW_DURATION_SECONDS", 0.05)
    db = _make_fake_db(tmp_path)
    assert await rbo._serve(db) == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_remote_build_only (blocking runner)
# ---------------------------------------------------------------------------


def test_runner_serves_until_graceful_exit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Already-paired service parks, then a stop signal exits 0 through db.stop()."""
    db = _make_fake_db(tmp_path, seed_approved_peer=True, raise_graceful_after_start=True)

    with caplog.at_level("INFO", logger=_RBO_LOGGER):
        rbo.run_remote_build_only(db)  # type: ignore[arg-type]

    assert db.started
    assert db.stopped
    assert any("already paired with 'Main builder'" in r.getMessage() for r in caplog.records)


def test_runner_raises_systemexit_1_when_not_serving(tmp_path: Path) -> None:
    """A listener that never bound exits 1 after teardown."""
    db = _make_fake_db(tmp_path, listener_bound=False)

    with pytest.raises(SystemExit) as excinfo:
        rbo.run_remote_build_only(db)  # type: ignore[arg-type]

    assert excinfo.value.code == 1
    assert db.stopped


def test_runner_logs_stop_failure_without_masking_exit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A teardown error is logged; the not-serving exit code still surfaces."""
    db = _make_fake_db(tmp_path, listener_bound=False, stop_error=RuntimeError("boom"))

    with (
        caplog.at_level("ERROR", logger=_RBO_LOGGER),
        pytest.raises(SystemExit) as excinfo,
    ):
        rbo.run_remote_build_only(db)  # type: ignore[arg-type]

    assert excinfo.value.code == 1
    assert any("Error during remote-build-only shutdown" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Lifecycle: --remote-build-only forces the listener on
# ---------------------------------------------------------------------------


async def test_maybe_start_binds_when_remote_build_only_overrides_disabled(
    tmp_path: Path,
) -> None:
    """A persisted ``enabled=False`` cannot brick the headless mode's listener."""
    loop = asyncio.get_running_loop()

    def _disable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = False

    await loop.run_in_executor(None, _disable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.remote_build_only = True
    settings.remote_build_port = 0  # ephemeral so the bind doesn't collide
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path
    db._remote_build_lifecycle.publish_advertise = AsyncMock()  # type: ignore[method-assign]

    try:
        await db._remote_build_lifecycle.maybe_start()
        assert db._remote_build_lifecycle._runner is not None
    finally:
        if db._remote_build_lifecycle._runner is not None:
            await db._remote_build_lifecycle._runner.cleanup()


# ---------------------------------------------------------------------------
# CLI / settings plumbing
# ---------------------------------------------------------------------------


def _ns(configuration: str, **kwargs: object) -> SimpleNamespace:
    """Minimal argparse-namespace stub for ``DashboardSettings.parse_args``."""
    defaults: dict[str, object] = {
        "ha_addon": False,
        "configuration": configuration,
        "username": "",
        "password": "",
        "log_level": "info",
        "port": 6052,
        "host": "0.0.0.0",
        "ingress_port": 6053,
        "ingress_host": "",
        "remote_build_port": None,
        "remote_build_host": None,
        "remote_build_only": False,
        "allow_pairing_source": "",
        "dev": False,
        "trusted_domains": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_parse_args_remote_build_only_defaults_off(tmp_path: Path) -> None:
    settings = DashboardSettings()
    settings.parse_args(_ns(configuration=str(tmp_path)))
    assert settings.remote_build_only is False
    assert settings.allow_pairing_sources == []


def test_normalize_pairing_sources_edge_cases() -> None:
    """Empty input yields []; blank and unparseable entries are dropped."""
    assert normalize_pairing_sources("") == []
    assert normalize_pairing_sources("  ,  ") == []
    # Invalid entries are dropped (the parser rejects them loudly upstream);
    # a valid entry alongside still survives.
    assert normalize_pairing_sources("not-an-ip, 192.168.1.5") == ["192.168.1.5"]


def test_parse_args_allow_pairing_sources_normalised(tmp_path: Path) -> None:
    """Comma-separated sources are split, IPv6-normalised, and deduplicated."""
    settings = DashboardSettings()
    settings.parse_args(
        _ns(
            configuration=str(tmp_path),
            remote_build_only=True,
            allow_pairing_source="192.168.1.5, 2001:0db8::1 ,192.168.1.5",
        )
    )
    assert settings.allow_pairing_sources == ["192.168.1.5", "2001:db8::1"]


def test_parse_args_remote_build_only_flag_sets_mode(tmp_path: Path) -> None:
    settings = DashboardSettings()
    settings.parse_args(_ns(configuration=str(tmp_path), remote_build_only=True))
    assert settings.remote_build_only is True


def test_main_rejects_remote_build_only_with_ha_addon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two run modes are mutually exclusive at the parser."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["esphome-device-builder", str(tmp_path), "--remote-build-only", "--ha-addon"],
    )
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_main_requires_config_dir_with_remote_build_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service started without an explicit config dir must refuse, not mint identity in cwd."""
    monkeypatch.setattr(sys, "argv", ["esphome-device-builder", "--remote-build-only"])
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_validate_mode_flags_defaults_config_dir_in_normal_mode() -> None:
    """Omitting the positional in normal mode keeps the ./configs default."""
    parser = argparse.ArgumentParser()
    args = SimpleNamespace(remote_build_only=False, ha_addon=False)
    main_module._validate_mode_flags(parser, args)
    assert args.configuration == "./configs"


def test_validate_mode_flags_keeps_explicit_config_dir() -> None:
    """An explicit path passes through untouched in remote-build-only mode."""
    parser = argparse.ArgumentParser()
    args = SimpleNamespace(
        remote_build_only=True,
        ha_addon=False,
        configuration="/var/lib/esphome-builder",
        allow_any_pairing_source=True,
    )
    main_module._validate_mode_flags(parser, args)
    assert args.configuration == "/var/lib/esphome-builder"


def test_main_rejects_allow_pairing_source_without_remote_build_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist only means something for the headless auto-approve window."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["esphome-device-builder", str(tmp_path), "--allow-pairing-source", "192.168.1.5"],
    )
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_main_rejects_invalid_allow_pairing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-IP allowlist entry is refused at the parser."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "esphome-device-builder",
            str(tmp_path),
            "--remote-build-only",
            "--allow-pairing-source",
            "not-an-ip",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_validate_mode_flags_accepts_valid_allowlist() -> None:
    """A valid IP allowlist with --remote-build-only passes validation.

    The trailing empty segment exercises the blank-entry skip.
    """
    parser = argparse.ArgumentParser()
    args = SimpleNamespace(
        remote_build_only=True,
        ha_addon=False,
        configuration="/var/lib/esphome-builder",
        allow_pairing_source="192.168.1.5, ::1, ",
        allow_any_pairing_source=False,
    )
    main_module._validate_mode_flags(parser, args)  # does not raise


def test_main_requires_pairing_source_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--remote-build-only with neither pairing-source flag is refused."""
    monkeypatch.setattr(
        sys, "argv", ["esphome-device-builder", str(tmp_path), "--remote-build-only"]
    )
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_main_rejects_both_pairing_source_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restrict and accept-any choices are mutually exclusive."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "esphome-device-builder",
            str(tmp_path),
            "--remote-build-only",
            "--allow-pairing-source",
            "192.168.1.5",
            "--allow-any-pairing-source",
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_main_rejects_allow_any_without_remote_build_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--allow-any-pairing-source is meaningless outside headless mode."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["esphome-device-builder", str(tmp_path), "--allow-any-pairing-source"],
    )
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_validate_mode_flags_accepts_allow_any() -> None:
    """--remote-build-only + --allow-any-pairing-source passes validation."""
    parser = argparse.ArgumentParser()
    args = SimpleNamespace(
        remote_build_only=True,
        ha_addon=False,
        configuration="/var/lib/esphome-builder",
        allow_pairing_source="",
        allow_any_pairing_source=True,
    )
    main_module._validate_mode_flags(parser, args)  # does not raise


def test_run_branches_into_headless_runner(make_settings: MakeSettingsFactory) -> None:
    """``DeviceBuilder.run`` hands off to the headless runner and binds no HTTP site."""
    settings = make_settings()
    settings.remote_build_only = True
    db = DeviceBuilder(settings)
    with (
        patch("esphome_device_builder.device_builder.run_remote_build_only") as runner,
        patch("esphome_device_builder.device_builder.web.run_app") as run_app,
    ):
        db.run()
    runner.assert_called_once_with(db)
    run_app.assert_not_called()


def test_warn_if_unprotected_skips_remote_build_only(
    make_settings: MakeSettingsFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """No HTTP dashboard is bound, so the no-auth dashboard banner would mislead."""
    settings = make_settings()
    settings.using_password = False
    settings.remote_build_only = True

    with caplog.at_level("WARNING", logger=main_module._LOGGER_NAME):
        main_module._warn_if_unprotected(settings)

    assert not any("WITHOUT AUTHENTICATION" in r.getMessage() for r in caplog.records)
